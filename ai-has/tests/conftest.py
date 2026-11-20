import os

# Dummy settings so the app can be imported without a real .env.
# The URL is never connected to: tests stub the database engine.
os.environ["DATABASE_URL"] = "postgresql+psycopg2://test:test@localhost:1/test"
os.environ["GEMINI_API_KEY"] = "test-key"
