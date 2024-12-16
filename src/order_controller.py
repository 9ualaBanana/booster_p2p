from asyncio import CancelledError, Event, Lock, create_task, sleep
from sqlalchemy import desc
from config import FROZEN_BALANCE_COOLDOWN
from database import Order, OrderStatus, SessionFactory
from telegram import Message

class OrderController:
    def __init__(self, notification: Message, user_data):
        self.notification = notification
        self.lock = Lock()
        self.event = Event()
        self.user_data = user_data
        self._confirmation_waiter = None

    async def start_confirmation_waiter(self):
        self._confirmation_waiter = create_task(self.order_confirmation_waiter())

    async def cancel_confirmation_waiter(self):
        if self._confirmation_waiter and not self._confirmation_waiter.cancelled():
            self._confirmation_waiter.cancel()

    async def order_confirmation_waiter(self):
        try:
            await sleep(FROZEN_BALANCE_COOLDOWN)
            async with self.lock:
                with SessionFactory() as session:
                    order = session.query(Order).filter_by(user_id=self.notification.chat_id).order_by(desc(Order.created_at)).first()
                    if order.status == OrderStatus.ACCEPTED:
                        order.user.balance += order.quantity
                        order.user.frozen_balance -= order.quantity
                        session.delete(order)
                        session.commit()
                        self.user_data.pop(Order.__name__)
                        await self.notification.edit_text(f"{self.notification.text}\n\nКлиент не совершил перевод по ордеру вовремя.\nОрдер отменён. Баланс разморожен.")
        except CancelledError:
            pass
