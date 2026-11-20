"""List the model names your API key can use.

Run with: python -m app.list_models
Use one of the printed names for CHAT_MODEL or EMBED_MODEL in .env.
"""
from .ingest import client


def main():
    names = sorted(m.id.removeprefix("models/") for m in client.models.list())
    for name in names:
        print(name)


if __name__ == "__main__":
    main()
