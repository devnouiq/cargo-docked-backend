from fastapi import Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy.orm import Session

from . import models
from .database import get_db

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def get_api_key(api_key_header: str = Security(api_key_header), db: Session = Depends(get_db)) -> models.Customer:
    if api_key_header:
        customer = db.query(models.Customer).filter(models.Customer.api_key == api_key_header).first()
        if customer:
            return customer
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API Key",
    )
