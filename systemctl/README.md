# How to setup the service

1. Update backlight_control.service details using 
   - "nano backlight_control.service
2. Copy it to /etc/systemd/system/backlight_control.service
3. Start the service
   - sudo systemctl daemon-reload
   - sudo systemctl enable myscript.service
   - sudo systemctl start myscript.service
4. Check status:
   - systemctl status myscript.service 
   - journalctl -u myscript.service -f
