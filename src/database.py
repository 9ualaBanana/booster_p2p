import uuid
from sqlalchemy import DateTime, create_engine, Column, BigInteger, String, Numeric, Boolean, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Mapped
from decimal import Decimal, ROUND_HALF_EVEN
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime, timezone
from typing import List
from enum import Enum

from formatting_helper import FormattingHelper

Base = declarative_base()

class OrderStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    COMPLETED = "completed"

class Currency(str, Enum):
    USD = "USD"
    EUR = "EUR"
    RUB = "RUB"

class User(Base):
    __tablename__ = 'users'
    
    id: Mapped[int] = Column(BigInteger, primary_key=True)
    name: Mapped[str] = Column(String)
    card: Mapped[str] = Column(String, nullable=False)
    balance: Mapped[Decimal] = Column(Numeric(precision=20, scale=8), nullable=False, default=Decimal(0))
    frozen_balance: Mapped[Decimal] = Column(Numeric(precision=20, scale=8), nullable=False, default=Decimal(0))
    exchange_rate: Mapped[Decimal] = Column(Numeric(precision=20, scale=8), nullable=False)
    currency: Mapped[Currency] = Column(String, nullable=False)
    is_working: Mapped[bool] = Column(Boolean, nullable=False, default=False)
    
    orders: Mapped[List["Order"]] = relationship("Order", back_populates="user")

    @property
    def formatted_name(self) -> str:
        max_length = 8
        if len(self.name) <= max_length:
            return self.name
        else:
            stripped_name = self.name[1:] if self.name.startswith('@') else self.name
            return stripped_name[:max_length] + "..."
    
    @property
    def formatted_exchange_rate(self) -> str:
        return str(0) if self.exchange_rate.is_zero() else FormattingHelper.quantize(self.exchange_rate, 2)
    
    @property
    def formatted_balance(self) -> str:
        return str(0) if self.balance.is_zero() else FormattingHelper.quantize(self.balance, 8)
    
class Order(Base):
    __tablename__ = 'orders'
    
    id: Mapped[str] = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    status: Mapped[str] = Column(String, nullable=False, default=OrderStatus.PENDING)
    price: Mapped[Decimal] = Column(Numeric(precision=20, scale=8), nullable=False)
    quantity: Mapped[Decimal] = Column(Numeric(precision=20, scale=8), nullable=False)
    paid_at: Mapped[datetime] = Column(DateTime(timezone=True))
    
    user_id: Mapped[int] = Column(BigInteger, ForeignKey('users.id'))
    user: Mapped["User"] = relationship("User", back_populates="orders")

    @property
    def total_price(self) -> Decimal:
        return self.price * self.quantity

# Pydantic models remain unchanged as they do not require `Mapped`
class UserModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    name: str
    card: str
    balance: Decimal = Field(default=Decimal(0))
    frozen_balance: Decimal = Field(default=Decimal(0))
    exchange_rate: Decimal
    is_working: bool = Field(default=False)
    orders: List['OrderModel']

class OrderModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: uuid.UUID = Field(default=uuid.uuid4())
    created_at: datetime = Field(default=datetime.now(timezone.utc))
    status: OrderStatus = OrderStatus.PENDING
    price: Decimal
    quantity: Decimal
    user_id: int
    user: UserModel

# engine and session factory setup remains unchanged
engine = create_engine('postgresql+psycopg://postgres:admin@localhost/p2p')
Base.metadata.create_all(engine)
SessionFactory = sessionmaker(bind=engine)
