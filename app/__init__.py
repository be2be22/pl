"""Aurora Edge Dashboard v3.0 — async control plane package.

v3 changelog:
  - Centralized all configuration & magic numbers in config.py
  - Replaced SHA256 password hashing with bcrypt
  - Added _proc_lock for thread-safe Xray subprocess access
  - Added exponential backoff for resync
  - Fixed log tail reading (now reads from end, not start)
  - Added caching for Axiom queries (60s TTL)
  - Added Pydantic request validation
  - Added security headers middleware
  - Added backpressure for Telegram webhook (semaphore)
  - Added proper task tracking & cancellation on shutdown
  - Fixed race conditions on state access
  - Shared HTTP client utility across all modules
  - Unified user mutation logic in services/user_service.py
  - HTML escaping in all bot messages
  - Pinned Xray version in Dockerfile
  - fsync after atomic writes
  - debounced local persistence
"""
__version__ = "3.1.0"
