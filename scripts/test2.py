#!/usr/bin/env python3
"""
Portfolio Weekly Report Generator
每週五自動執行：抓台股/美股收盤價 → 計算損益 → 生成 HTML 週報
"""

import json
import os
import sys
import time
import datetime
import requests
from pathlib import Path