"""区间分摊编译器：把计量区间拆成可解释的计费事实片段。

硬规则（对应题面）：
1. 跨越租约/转包生效点：按归属沿革拆段，绝不按区间末尾的当前归属整段计价；
   无中间读数时按时间比例分摊并显式标注 split_basis=time_proportional。
2. 一条累计读数跨越两个价目版本且边界处缺中间有效读数：
   不按任一价目猜测拆分，发布 cross_price_pending 待证片段（blocked_pending），
   阻断其进入确认对账；边界存在读数时按读数锚定拆分。
3. 转包生效日与人工补录读数区间矛盾：登记 AttributionConflict，
   同一 conflict_group 下并列两套 attribution_disputed 候选片段及金额范围，
   凭证裁定前不进确认对账；裁定后重编译按结论转确认片段。
4. 阶梯价按账期累计量落在的边际阶梯计价，跨阶梯边界再拆 tier_split 片段；
   减免规则随价目版本固化到片段。
5. 全部片段追加写入，重编译以版本链取代旧片段，旧行保留可回放。

读数锚定时间线：以事实起止读数与价目边界处的有效读数构造锚点区间，
锚点区间之间水量由真实读数差确定；任何价目边界缺读数即整段待证，
因此「确认片段」永远不会跨越一个没有读数支撑的价目边界。
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ManualRevision, RawMessage, SupplyFact
from ..models_billing import (
    AttributionConflict, BillingFragment, PriceVersion, TenureRecord,
    FRAG_ATTRIBUTION_DISPUTED, FRAG_BLOCKED, FRAG_CANDIDATE, FRAG_CONFIRMED,
    FRAG_CROSS_PRICE_PENDING, FRAG_CROSS_TENURE, FRAG_NORMAL, FRAG_TIER_SPLIT,
)
from . import billing_master as bm
from .billing_common import money, period_key_of, uid, volume
from .billing_ledger import supersede


# ---------------------------------------------------------------- 读数序列
def device_reading_series(db: Session, device_id: str) -> list[dict]:
    """同事件时间取最高 revision 读数，返回 [{event_time, reading}]。"""
    msgs = db.scalars(select(RawMessage).where(
        RawMessage.device_id == device_id).order_by(RawMessage.event_time)).all()
    bucket: dict[datetime, dict] = {}
    for m in msgs:
        r = float(m.payload.get("reading", 0.0))
        cur = bucket.get(m.event_time)
        if cur is None or m.revision_no > cur["revision_no"]:
            bucket[m.event_time] = {"event_time": m.event_time, "reading": r,
                                    "revision_no": m.revision_no}
    return [bucket[k] for k in sorted(bucket)]


def _manual_claimed_operator(db: Session, fact: SupplyFact) -> str | None:
    """人工补录主张的归属经营户（记录在补录 payload 中）。"""
    for mid in fact.evidence_manual_ids or []:
        rev = db.get(ManualRevision, mid)
        if rev and rev.payload.get("claimed_operator_code"):
            return rev.payload["claimed_operator_code"]
    return None


def _span_ratio(s: datetime, e: datetime, a: datetime, b: datetime) -> float:
    total = (e - s).total_seconds()
    return ((b - a).total_seconds() / total) if total > 0 else 0.0


def _segments(s: datetime, e: datetime, ranges):
    """按生效区间把 [s,e) 切成与单个 SCD2 行对齐的子段。ranges: [(from,to,row)]。"""
    cuts = sorted({s, e} | {r[0] for r in ranges if s < r[0] < e})
    out = []
    for a, b in zip(cuts, cuts[1:]):
        row = next((r for r in ranges if r[0] <= a and (r[1] is None or r[1] > a)), None)
        if row:
            out.append((a, b, row[2]))
    return out


# ---------------------------------------------------------------- 片段构造
class FragmentBuilder:
    """按账期累计量处理阶梯计价与减免，输出片段行。

    确认片段与每个归属候选各自使用独立的累计量空间，
    避免双候选的假设水量互相污染阶梯档位。
    """

    def __init__(self, db: Session):
        self.db = db
        self._cumulative: dict[str, dict[tuple, float]] = {"__confirmed__": {}}
        self._fixed_relief: dict[str, set[tuple]] = {"__confirmed__": set()}

    def _scope(self, pricing_key: str | None) -> str:
        key = pricing_key or "__confirmed__"
        self._cumulative.setdefault(key, {})
        self._fixed_relief.setdefault(key, set())
        return key

    def _relief(self, scope: str, pv: PriceVersion, plot_code: str,
                base_amount: float, operator_code: str | None = None):
        rule = bm.applicable_relief(pv, plot_code, operator_code)
        if rule is None or base_amount <= 0:
            return None, 0.0
        if rule.relief_kind == "rate":
            return rule, money(base_amount * (rule.rate or 0.0))
        key = (plot_code, pv.version_no, rule.id)
        if key in self._fixed_relief[scope]:
            return None, 0.0
        self._fixed_relief[scope].add(key)
        return rule, money(min(rule.amount or 0.0, base_amount))

    def emit(self, *, fact: SupplyFact, fragment_kind: str, status: str,
             start: datetime, end: datetime, vol: float, split_basis: str,
             tenure: TenureRecord | None, pv: PriceVersion | None,
             blocking_reason: str | None = None, conflict_group_uid: str | None = None,
             pricing_key: str | None = None, detail: dict | None = None
             ) -> list[BillingFragment]:
        scope = self._scope(pricing_key)
        common = dict(
            period_key=period_key_of(start), fragment_kind=fragment_kind, status=status,
            supply_fact_id=fact.id, device_id=fact.device_id,
            meter_point_code=fact.meter_point_code, plot_code=fact.plot_code,
            interval_start=start, interval_end=end,
            split_basis=split_basis, blocking_reason=blocking_reason,
            tenure_id=tenure.id if tenure else None,
            tenure_version=tenure.tenure_version if tenure else None,
            operator_code=tenure.operator_code if tenure else None,
            lease_code=tenure.lease_code if tenure else None,
            price_version_id=pv.id if pv else None,
            price_version_no=pv.version_no if pv else None,
            conflict_group_uid=conflict_group_uid,
            evidence_message_ids=list(fact.evidence_message_ids or []),
            evidence_manual_ids=list(fact.evidence_manual_ids or []),
            detail=detail or {})

        # 阻断/候选无有效价目依据：不给金额
        if pv is None or status == FRAG_BLOCKED:
            f = BillingFragment(
                fragment_uid=uid("frag"), volume=volume(vol),
                tier_no=None, unit_price=None, base_amount=None,
                relief_amount=None, amount=None, **common)
            self.db.add(f); self.db.flush()
            return [f]

        # 阶梯边际切片（累计量在独立 scope 内推进）
        cum_key = (fact.plot_code, pv.version_no)
        used = self._cumulative[scope].get(cum_key, 0.0)
        tiers = sorted(pv.tiers, key=lambda t: t.volume_from)
        remaining, cursor = vol, used
        rows: list[BillingFragment] = []
        for tier in tiers:
            if remaining <= 1e-9:
                break
            upper = tier.volume_to
            cap = (upper - cursor) if upper is not None else remaining
            if cap <= 0:
                continue
            slice_vol = min(remaining, max(cap, 0.0))
            base = money(slice_vol * tier.unit_price)
            rule, relief = self._relief(scope, pv, fact.plot_code, base,
                                        tenure.operator_code if tenure else None)
            row = BillingFragment(
                **dict(common, fragment_uid=uid("frag"),
                       fragment_kind=FRAG_TIER_SPLIT
                       if (slice_vol != vol or used > 0) else fragment_kind,
                       volume=volume(slice_vol), tier_no=tier.tier_no,
                       unit_price=tier.unit_price, base_amount=base,
                       relief_amount=money(relief),
                       amount=money(base - relief),
                       relief_rule_id=rule.id if rule else None))
            self.db.add(row); self.db.flush()
            rows.append(row)
            remaining -= slice_vol
            cursor += slice_vol
        if not rows:  # 水量超出全部阶梯且无敞开档：阻断待证，不臆造价格
            f = BillingFragment(
                **dict(common, fragment_uid=uid("frag"), volume=volume(vol),
                       blocking_reason=blocking_reason or "TIER_NOT_COVERED",
                       status=FRAG_BLOCKED, tier_no=None, unit_price=None,
                       base_amount=None, relief_amount=None, amount=None))
            self.db.add(f); self.db.flush()
            return [f]
        self._cumulative[scope][cum_key] = used + vol
        return rows


# ---------------------------------------------------------------- 读数锚定时间线
def _anchor_timeline(fact: SupplyFact, series: list[dict], boundaries: list[datetime]):
    """构造锚点区间。

    返回 (anchors, volumes) 或 None：
    - 任一价目边界缺同刻有效读数 => None（调用方发待证片段）；
    - 否则锚点 = 事实起止 + 全部边界，区间水量按累计读数差比例分配。
    """
    r_s = fact.reading_start
    r_e = fact.reading_end
    reading_at = {p["event_time"]: p["reading"] for p in series}
    pts = [fact.interval_start] + [b for b in boundaries
                                   if fact.interval_start < b < fact.interval_end] \
        + [fact.interval_end]
    pts = sorted(set(pts))
    readings = []
    for p in pts:
        if p in (fact.interval_start, fact.interval_end):
            readings.append(r_s if p == fact.interval_start else r_e)
            continue
        if p not in reading_at:
            return None
        readings.append(reading_at[p])
    if r_s is None or r_e is None or r_e - r_s <= 0:
        return None
    V = max(fact.value or 0.0, 0.0)
    denom = r_e - r_s
    vols = [volume(V * (readings[i + 1] - readings[i]) / denom)
            for i in range(len(pts) - 1)]
    # 修正末段舍入，保证锚点区间水量之和等于事实水量
    drift = volume(V - sum(vols))
    if vols:
        vols[-1] = volume(vols[-1] + drift)
    return list(zip(pts, pts[1:])), vols


def _clip_volume(fact: SupplyFact, anchors, vols, a: datetime, b: datetime) -> float:
    """把锚点时间线的水量裁剪到 [a,b)；锚点区间内部按时间比例（均匀流量假设）。"""
    total = 0.0
    for (x, y), v in zip(anchors, vols):
        s, e = max(x, a), min(y, b)
        if s < e:
            total += v * _span_ratio(x, y, s, e)
    return volume(total)


# ---------------------------------------------------------------- 单事实编译
def compile_fact(db: Session, fact: SupplyFact, builder: FragmentBuilder,
                 series: list[dict]) -> list[BillingFragment]:
    s, e = fact.interval_start, fact.interval_end
    V = max(fact.value or 0.0, 0.0)
    tenures = bm.tenures_overlapping(db, fact.plot_code, s, e)
    prices = bm.price_versions_overlapping(db, s, e, fact.plot_code)

    # 源事实未确认：阻断，不进确认对账
    if fact.status != "confirmed":
        return builder.emit(
            fact=fact, fragment_kind=FRAG_NORMAL, status=FRAG_BLOCKED,
            start=s, end=e, vol=V, split_basis="pending_evidence",
            tenure=tenures[0] if tenures else None,
            pv=prices[0] if prices else None,
            blocking_reason=f"SOURCE_FACT_{fact.status.upper()}",
            detail={"source_fact_status": fact.status,
                    "quality_flag": fact.quality_flag})

    if not tenures:
        return builder.emit(
            fact=fact, fragment_kind=FRAG_NORMAL, status=FRAG_BLOCKED,
            start=s, end=e, vol=V, split_basis="pending_evidence",
            tenure=None, pv=prices[0] if prices else None,
            blocking_reason="NO_TENURE_RECORD",
            detail={"reason": "区间内无有效归属沿革，无法确定用水经营户"})

    # ---- 归属冲突：转包边界与人工补录区间矛盾 ----
    claimed = _manual_claimed_operator(db, fact)
    tenure_boundaries = [t.effective_from for t in tenures if s < t.effective_from < e]
    if claimed and tenure_boundaries:
        return _compile_attribution_dispute(
            db, fact, builder, tenures, prices, series, V)

    out: list[BillingFragment] = []
    tenure_segments = _segments(
        s, e, [(t.effective_from, t.effective_to, t) for t in tenures])
    multi_tenure = len(tenure_segments) > 1

    for seg_start, seg_end, tenure in tenure_segments:
        seg_prices = sorted(
            (p for p in prices
             if p.effective_from < seg_end and
             (p.effective_to is None or p.effective_to > seg_start)),
            key=lambda p: p.effective_from)

        # 该归属段内的价目边界
        boundaries = [p.effective_from for p in seg_prices
                      if seg_start < p.effective_from < seg_end]
        timeline = _anchor_timeline(fact, series, boundaries) if boundaries else None
        if boundaries and timeline is None:
            # 跨价目且边界缺中间有效读数：整段待证，不按任一价目猜测
            out += builder.emit(
                fact=fact, fragment_kind=FRAG_CROSS_PRICE_PENDING,
                status=FRAG_BLOCKED, start=seg_start, end=seg_end,
                vol=volume(V * _span_ratio(s, e, seg_start, seg_end)),
                split_basis="pending_evidence", tenure=tenure,
                pv=seg_prices[0], blocking_reason="CROSS_PRICE_NO_INTERIM_READING",
                detail={"price_boundaries": [b.isoformat() for b in boundaries],
                        "price_versions": [p.version_no for p in seg_prices],
                        "reason": "累计读数跨价目版本但边界缺中间有效读数，"
                                  "不得按任一价目猜测拆分"})
            continue

        for idx, pv in enumerate(seg_prices):
            a = max(seg_start, pv.effective_from)
            b = min(seg_end, pv.effective_to or seg_end)
            if a >= b:
                continue
            if timeline is not None:
                part_vol = _clip_volume(fact, timeline[0], timeline[1], a, b)
                basis = "reading_anchored"
            elif multi_tenure:
                part_vol = volume(V * _span_ratio(s, e, a, b))
                basis = "time_proportional"
            else:
                part_vol, basis = V, "reading_anchored"
            kind = FRAG_CROSS_TENURE if multi_tenure else FRAG_NORMAL
            out += builder.emit(
                fact=fact, fragment_kind=kind, status=FRAG_CONFIRMED,
                start=a, end=b, vol=part_vol, split_basis=basis,
                tenure=tenure, pv=pv,
                detail={"tenure_split": multi_tenure, "tenure_uid": tenure.tenure_uid,
                        "price_version_no": pv.version_no})
    return out


# ---------------------------------------------------------------- 归属双候选
def _compile_attribution_dispute(db: Session, fact: SupplyFact, builder: FragmentBuilder,
                                 tenures: list[TenureRecord], prices: list[PriceVersion],
                                 series, V: float) -> list[BillingFragment]:
    boundary = next(t.effective_from for t in tenures
                    if fact.interval_start < t.effective_from < fact.interval_end)
    # 冲突组用设备+计量区间的稳定自然键，重编译产生新事实行后仍能关联同一冲突
    group_uid = (f"acg-{fact.device_id or fact.plot_code}-"
                 f"{fact.interval_start.isoformat()}-{fact.interval_end.isoformat()}")
    existing = db.scalar(select(AttributionConflict).where(
        AttributionConflict.conflict_group_uid == group_uid))

    # 已裁定：按结论生成单一确认归属
    if existing and existing.status == "adjudicated":
        if existing.evidence_refs.get("decision") == "accept_claim":
            claimed = existing.claimed_operator_code
            chosen = next((t for t in tenures if t.operator_code == claimed),
                          tenures[-1])
            return builder.emit(
                fact=fact, fragment_kind=FRAG_NORMAL, status=FRAG_CONFIRMED,
                start=fact.interval_start, end=fact.interval_end, vol=V,
                split_basis="adjudicated_claim", tenure=chosen,
                pv=prices[0] if prices else None, conflict_group_uid=group_uid,
                detail={"adjudicated": "accept_claim", "conflict_group": group_uid})
        return _tenure_split_confirmed(fact, builder, tenures, prices, V, group_uid)

    conflict_rows: list[BillingFragment] = []
    # 候选 A：沿革归属（按转包边界时间比例拆段），独立计价空间
    amount_a = 0.0
    for seg_start, seg_end, tenure in _segments(
            fact.interval_start, fact.interval_end,
            [(t.effective_from, t.effective_to, t) for t in tenures]):
        pv = next((p for p in prices
                   if p.effective_from < seg_end and
                   (p.effective_to is None or p.effective_to > seg_start)), None)
        rows = builder.emit(
            fact=fact, fragment_kind=FRAG_ATTRIBUTION_DISPUTED, status=FRAG_CANDIDATE,
            start=seg_start, end=seg_end,
            vol=volume(V * _span_ratio(fact.interval_start, fact.interval_end,
                                       seg_start, seg_end)),
            split_basis="time_proportional", tenure=tenure, pv=pv,
            conflict_group_uid=group_uid, pricing_key=f"{group_uid}#tenure",
            detail={"candidate": "tenure_record", "conflict_group": group_uid})
        conflict_rows += rows
        amount_a += sum(r.amount or 0.0 for r in rows)

    # 候选 B：人工补录主张经营户整段归属，独立计价空间
    claimed = existing.claimed_operator_code if existing \
        else _manual_claimed_operator(db, fact)
    claimed_tenure = next((t for t in tenures
                           if t.operator_code == claimed), None)
    amount_b = 0.0
    if claimed_tenure and prices:
        rows = builder.emit(
            fact=fact, fragment_kind=FRAG_ATTRIBUTION_DISPUTED, status=FRAG_CANDIDATE,
            start=fact.interval_start, end=fact.interval_end, vol=V,
            split_basis="manual_claim_whole_interval", tenure=claimed_tenure,
            pv=prices[0], conflict_group_uid=group_uid,
            pricing_key=f"{group_uid}#claim",
            detail={"candidate": "manual_claim", "claimed_operator": claimed,
                    "conflict_group": group_uid})
        conflict_rows += rows
        amount_b = sum(r.amount or 0.0 for r in rows)

    lower, upper = money(min(amount_a, amount_b)), money(max(amount_a, amount_b))
    if existing is None:
        db.add(AttributionConflict(
            conflict_uid=uid("acf"), conflict_group_uid=group_uid,
            period_key=period_key_of(fact.interval_start), plot_code=fact.plot_code,
            interval_start=fact.interval_start, interval_end=fact.interval_end,
            tenure_operator_code=tenures[-1].operator_code,
            claimed_operator_code=claimed or "",
            amount_lower=lower, amount_upper=upper, status="pending",
            evidence_refs={"manual_ids": list(fact.evidence_manual_ids or []),
                           "tenure_boundary": boundary.isoformat()}))
        db.flush()
    else:
        existing.amount_lower, existing.amount_upper = lower, upper
    return conflict_rows


def _tenure_split_confirmed(fact, builder, tenures, prices, V, group_uid):
    out = []
    for seg_start, seg_end, tenure in _segments(
            fact.interval_start, fact.interval_end,
            [(t.effective_from, t.effective_to, t) for t in tenures]):
        pv = next((p for p in prices
                   if p.effective_from < seg_end and
                   (p.effective_to is None or p.effective_to > seg_start)), None)
        out += builder.emit(
            fact=fact, fragment_kind=FRAG_CROSS_TENURE, status=FRAG_CONFIRMED,
            start=seg_start, end=seg_end,
            vol=volume(V * _span_ratio(fact.interval_start, fact.interval_end,
                                       seg_start, seg_end)),
            split_basis="adjudicated_tenure", tenure=tenure, pv=pv,
            conflict_group_uid=group_uid,
            detail={"adjudicated": "accept_tenure", "conflict_group": group_uid})
    return out


def _select_finest_facts(facts: list) -> list:
    """同一设备存在重叠事实时，仅保留最细粒度分区。

    迟到补传补入中间读数后，计量账会同时存在旧的粗区间事实（如 0-4 点）
    与新的细区间事实（0-2、2-4 点）；粗事实不会被基线按端点取代，
    但计费不得重复计价。若其他事实的区间并集完整覆盖某事实区间，
    则该粗事实退出计费编译（计量账中仍保留可审计）。
    """
    by_device: dict[str, list] = {}
    standalone = []
    for f in facts:
        if f.device_id:
            by_device.setdefault(f.device_id, []).append(f)
        else:
            standalone.append(f)
    kept: list = []
    for dev, rows in by_device.items():
        rows = sorted(rows, key=lambda x: (x.interval_start, x.interval_end))
        for f in rows:
            # 仅以严格包含于 f 区间内的更细事实作为覆盖候选，
            # 避免粗区间事实反向把细区间事实判为冗余
            finer = [o for o in rows
                     if o is not f and o.fact_kind == f.fact_kind
                     and o.interval_start >= f.interval_start
                     and o.interval_end <= f.interval_end
                     and (o.interval_start > f.interval_start
                          or o.interval_end < f.interval_end)]
            if not _union_covers(finer, f.interval_start, f.interval_end):
                kept.append(f)
    return kept + standalone


def _union_covers(rows, start, end) -> bool:
    """rows 的区间并集是否完整覆盖 [start, end)。"""
    intervals = sorted((max(r.interval_start, start), min(r.interval_end, end))
                       for r in rows if r.interval_start < end and r.interval_end > start)
    cursor = start
    for a, b in intervals:
        if a > cursor:
            return False
        if b > cursor:
            cursor = b
        if cursor >= end:
            return True
    return cursor >= end


# ---------------------------------------------------------------- 全量入口
def compile_billing(db: Session, period_start: datetime, period_end: datetime,
                    plot_codes: list[str] | None = None) -> dict:
    """编译账期内全部活动供水事实为计费片段（追加式取代旧片段）。"""
    from .ledger import active_facts
    from .billing_ledger import superseded_fragment_ids

    raw_facts = [f for f in active_facts(db)
                 if f.fact_kind == "supply" and f.plot_code
                 and f.interval_start < period_end and f.interval_end > period_start
                 and (not plot_codes or f.plot_code in plot_codes)]
    # 中间读数补传后剔除被细区间事实完整覆盖的粗区间事实，避免重复计价
    facts = _select_finest_facts(raw_facts)
    builder = FragmentBuilder(db)
    series_cache: dict[str, list[dict]] = {}
    produced: list[BillingFragment] = []
    for fact in facts:
        if fact.device_id and fact.device_id not in series_cache:
            series_cache[fact.device_id] = device_reading_series(db, fact.device_id)
        produced += compile_fact(
            db, fact, builder, series_cache.get(fact.device_id, []))

    # 版本链：同地块旧活动片段由该地块的新片段取代（排除本批新片段自身），
    # 避免跨地块把无关片段挂进同一条取代链
    dead = superseded_fragment_ids(db)
    new_ids = {f.id for f in produced}
    produced_by_plot: dict[str, list[BillingFragment]] = {}
    for f in produced:
        produced_by_plot.setdefault(f.plot_code, []).append(f)
    affected_plots = {f.plot_code for f in facts}
    priors = [f for f in db.scalars(select(BillingFragment)).all()
              if f.id not in dead and f.id not in new_ids
              and f.plot_code in affected_plots
              and f.interval_start < period_end and f.interval_end > period_start]
    for old in priors:
        head = produced_by_plot.get(old.plot_code)
        if head:
            supersede(head[0], old)
    db.flush()
    return {
        "fragments_written": len(produced),
        "superseded": len(priors),
        "blocked": len({f.id for f in produced if f.status == FRAG_BLOCKED}),
        "candidates": len({f.id for f in produced if f.status == FRAG_CANDIDATE}),
        "confirmed": len({f.id for f in produced if f.status == FRAG_CONFIRMED}),
        "fragment_ids": [f.id for f in produced],
    }
