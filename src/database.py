from sqlalchemy import create_engine, Column, BigInteger, String, Float, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    name = Column(String)
    card = Column(String, nullable=False)
    balance = Column(Float, default=0)
    frozen_balance = Column(Float, default=0)
    exchange_rate = Column(Float, nullable=False)
    is_working = Column(Boolean, default=False)

    @property
    def formatted_name(self):
        max_length = 8
        if len(self.name) <= max_length:
            return self.name
        else:
            stripped_name = self.name[1:] if self.name.startswith('@') else self.name
            return stripped_name[:max_length] + "..."

# engine = create_engine('sqlite:///p2p.db')
engine = create_engine('postgresql+psycopg://postgres:admin@localhost/p2p')
Base.metadata.create_all(engine)
SessionFactory = sessionmaker(bind=engine)
