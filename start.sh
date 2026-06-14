#!/bin/bash
# Запускаем оба агента параллельно
python -u agent.py &
python -u vk_monitor.py &
wait
