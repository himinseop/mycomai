#!/bin/bash

# Export environment variables for cron
printenv | grep -v "no_proxy" > /etc/environment

# Start the cron service
service cron start

# Create the log file to be able to run tail
touch /var/log/cron.log

echo "Cron scheduler started..."
# Tail the cron logs to keep the container running
tail -f /var/log/cron.log
