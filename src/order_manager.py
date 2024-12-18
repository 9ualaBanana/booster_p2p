from asyncio import CancelledError, Event, Lock, create_task, sleep
from typing import Mapping, Optional, Self

from telegram import Message
from config import FROZEN_BALANCE_COOLDOWN
from database import Order, OrderStatus, SessionFactory
import logging

class OrderContext:
    def __init__(self, user_id: int, ocm: 'OrderContextManager'):
        self._order_id = None
        self.order = None
        self._user_id = user_id
        self._lock = Lock()
        self.event = Event()
        self.session = None
        self.notification: Message = None
        self.support_message: Message = None
        self._client_completion_waiter = None
        self._ocm = ocm

    async def __aenter__(self) -> Self:
        await self._lock.acquire()
        try:
            self.session = SessionFactory()
            if self._order_id:
                self.order = self.session.query(Order).filter_by(id=self._order_id).one()
        except Exception as e:
            logging.error(f"Error during OrderContext __aenter__ for user {self._user_id}: {e}", exc_info=True)
            raise
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type is not None:
                if self.session:
                    logging.debug(f"Rolling back session for user {self._user_id} due to exception: {exc_type}")
                    self.session.rollback()
                self.session.rollback()
        finally:
            if self.session:
                self.session.close()
                self.session = None
            self._lock.release()

    async def start_client_completion_waiter(self):
        self._client_completion_waiter = create_task(self.client_completion_waiter())

    async def cancel_client_completion_waiter(self):
        try:
            self._client_completion_waiter.cancel()
        except:
            pass

    async def client_completion_waiter(self):
        try:
            await sleep(FROZEN_BALANCE_COOLDOWN)
            async with self:
                order = self.order
                if order.status == OrderStatus.ACCEPTED:
                    order.user.balance += order.quantity
                    order.user.frozen_balance -= order.quantity
                    self.session.delete(order)
                    self.session.commit()
                    self._ocm.remove_context()
                    await self.notification.edit_text(f"{self.notification.text}\n\nКлиент не совершил перевод по ордеру вовремя.\nОрдер отменён. Баланс разморожен.")
        except CancelledError:
            pass


class OrderContextManager:
    global_lock = Lock()

    @classmethod
    async def get(cls, user_id: int, user_data: Mapping[int, dict[str, dict]]) -> 'OrderContextManager':
        try:
            async with cls.global_lock:
                return user_data[user_id].setdefault(OrderContextManager.__name__, OrderContextManager(user_id, user_data))
        except Exception as e:
            logging.error(f"Error getting {OrderContextManager.__name__} for user {user_id}: {e}", exc_info=True)
            raise
        
    def __init__(self, id: int, user_data: Mapping[int, dict[str, dict]]):
        self._lock = Lock()
        self.id = id
        self.user_data = user_data

    # Locks seting/getting `OrderContext` this `OrderContextManager` manages.
    async def __aenter__(self) -> Self:
        await self._lock.acquire()
        return self
    
    async def __aexit__(self, exc_type, exc_value, traceback):
        self._lock.release()
    
    @property
    def context(self) -> Optional[OrderContext]:
        return self.user_data[self.id].get(OrderContext.__name__)
    
    def create_context(self) -> OrderContext:
        return self.user_data[self.id].setdefault(OrderContext.__name__, OrderContext(self.id, self))
    
    def remove_context(self):
        self.user_data[self.id].pop(OrderContext.__name__)
