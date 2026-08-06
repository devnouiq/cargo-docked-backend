from sqlalchemy import Column, Integer, String, DateTime, JSON
from datetime import datetime, timezone
from .database import Base

class ContainerResult(Base):
    __tablename__ = "container_results"

    id = Column(Integer, primary_key=True, index=True)
    container_number = Column(String, index=True, unique=True)
    status = Column(String)
    location = Column(String)
    raw_data = Column(JSON)  # Store the full scraped JSON
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    api_key = Column(String, unique=True, index=True)
    daily_limit = Column(Integer, default=1000)
    requests_today = Column(Integer, default=0)
    last_reset = Column(DateTime, default=lambda: datetime.now(timezone.utc))
