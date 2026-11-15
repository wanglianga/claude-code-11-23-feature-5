import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DataContract
from ..schemas import ContractIn, ContractOut
from ..services.ingestion import register_contract

router = APIRouter(prefix="/api/contracts", tags=["数据契约"])


@router.post("", response_model=ContractOut, status_code=201)
def create_contract(data: ContractIn, db: Session = Depends(get_db)):
    try:
        return register_contract(db, data)
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("", response_model=list[ContractOut])
def list_contracts(db: Session = Depends(get_db)):
    return list(db.scalars(select(DataContract).order_by(DataContract.id)).all())
