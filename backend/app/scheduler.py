from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from app.tasks import broker

scheduler = TaskiqScheduler(broker, sources=[LabelScheduleSource(broker)])
