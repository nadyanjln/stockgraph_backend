import os
import sys

# Ensure backend root directory is in sys.path for Vercel Serverless Function imports
backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_root not in sys.path:
    sys.path.insert(0, backend_root)

from app.main import app
