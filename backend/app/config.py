from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/irrigation"
    stability_minutes: int = 30          # 发布稳定期
    allowed_lateness_minutes: int = 15   # 允许迟到时长（水位线）
    clock_drift_minutes: int = 30        # 时钟漂移阈值
    imbalance_tolerance_ratio: float = 0.05  # 渠道守恒容差 5%
    missing_gap_factor: float = 1.5      # 间隔超过 1.5 倍采集区间判缺测
    app_name: str = "灌区多源用水事实数据与指标发布平台"

    class Config:
        env_file = ".env"
        env_prefix = "IRR_"


settings = Settings()
