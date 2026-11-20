import json
from pathlib import Path

from sqlalchemy import text

from .config import settings
from .db import engine
from .ingest import client

OUT = Path(__file__).parent / "eval_data"
PER_ARTICLE = 3

# Questions the KB should NOT answer: the assistant should escalate these.
OUT_OF_SCOPE = [
    "What is a good recipe for chicken adobo?",
    "Who won the NBA finals last year?",
    "How do I file my income tax return?",
    "What is the weather in Manila today?",
    "Can you translate 'good morning' into Japanese?",
    "How much is the salary of a software engineer in Canada?",
]


def main():
    OUT.mkdir(exist_ok=True)
    with engine.connect() as conn:
        rows = conn.execute(
            text("select metadata->>'source' as source, content from kb_chunks order by id")
        ).mappings().all()

    docs: dict[str, list[str]] = {}
    for r in rows:
        docs.setdefault(r["source"], []).append(r["content"])

    cases = []
    for source, chunks in docs.items():
        body = "\n\n".join(chunks)[:3500]
        res = client.chat.completions.create(
            model=settings.chat_model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Below is an IT helpdesk article. Write {PER_ARTICLE} different questions "
                        "an employee might ask that this article answers. Use casual, natural "
                        "wording, not the article's headings. One question per line, no numbering, "
                        f"no extra text.\n\nARTICLE:\n{body}"
                    ),
                }
            ],
        )
        lines = [
            line.strip().lstrip("-*0123456789. )").strip()
            for line in res.choices[0].message.content.splitlines()
        ]
        for q in [ln for ln in lines if len(ln) > 10][:PER_ARTICLE]:
            cases.append({"question": q, "expected_source": source, "in_scope": True})
        print(f"[gen] {source}: ok", flush=True)

    for q in OUT_OF_SCOPE:
        cases.append({"question": q, "expected_source": None, "in_scope": False})

    (OUT / "questions.json").write_text(json.dumps(cases, indent=2, ensure_ascii=False))
    print(f"Saved {len(cases)} questions to {OUT / 'questions.json'}")


if __name__ == "__main__":
    main()