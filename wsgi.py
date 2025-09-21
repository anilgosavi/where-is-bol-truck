#!/usr/bin/env python3
"""
WSGI entry point for Render.com deployment
"""
import os
from simple_app_fixed import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
