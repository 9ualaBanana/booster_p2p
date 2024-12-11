from sqlalchemy import create_engine, Column, BigInteger, String, Numeric, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base
from decimal import Decimal, ROUND_HALF_EVEN

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    name = Column(String)
    card = Column(String, nullable=False)
    balance = Column(Numeric(precision=20, scale=8), nullable=False, default=Decimal(0))
    frozen_balance = Column(Numeric(precision=20, scale=8), nullable=False, default=Decimal(0))
    exchange_rate = Column(Numeric(precision=20, scale=8), nullable=False)
    is_working = Column(Boolean, nullable=False, default=False)

    @property
    def formatted_name(self):
        max_length = 8
        if len(self.name) <= max_length:
            return self.name
        else:
            stripped_name = self.name[1:] if self.name.startswith('@') else self.name
            return stripped_name[:max_length] + "..."
    
    @property
    def formatted_exchange_rate(self) -> str:
        return str(self.exchange_rate.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)).rstrip('0').rstrip('.')
    
    @property
    def formatted_balance(self) -> str:
        return str(self.balance.quantize(Decimal('0.000001'), rounding=ROUND_HALF_EVEN)).rstrip('0').rstrip('.')
    
    @property
    def formatted_frozen_balance(self) -> str:
        return str(self.frozen_balance.quantize(Decimal('0.000001'), rounding=ROUND_HALF_EVEN)).rstrip('0').rstrip('.')

# engine = create_engine('sqlite:///p2p.db')
engine = create_engine('postgresql+psycopg://postgres:admin@localhost/p2p')
Base.metadata.create_all(engine)
SessionFactory = sessionmaker(bind=engine)
