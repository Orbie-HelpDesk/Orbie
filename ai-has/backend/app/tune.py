"""Find a good MAX_DISTANCE by checking real questions against the KB.

Run (backend container must be up):
    docker compose exec backend python -m app.tune
"""

from .rag import MAX_DISTANCE, retrieve

# Questions that SHOULD be answered from the KB
IN_KB = [
    "My printer is not working",
    "Printer won't print",
    "How can I restart my computer?",
    "My computer is slow",
    "I forgot my password",
    "My account is locked",
    "My VPN won't connect",
    "I need access to Figma",
    "My Wi-Fi keeps disconnecting",
    "No internet connection",
    "How do I set up my email on my phone?",
]

# Questions that should NOT be answered (not in the KB)
OUT_KB = [
    "What's the best pizza in Manila?",
    "Who won the basketball game last night?",
    "How do I cook adobo?",
    "What is the weather today?",
]


def best(question: str):
    chunks = retrieve(question)
    if not chunks:
        return 9.9, "-"
    top = min(chunks, key=lambda c: c["distance"])
    return top["distance"], top["metadata"].get("source", "?")


def run(label: str, questions: list[str]) -> list[float]:
    print(f"\n== {label} ==")
    values = []
    for q in questions:
        d, src = best(q)
        values.append(d)
        verdict = "answer" if d <= MAX_DISTANCE else "NOT FOUND"
        print(f"{d:.3f}  {verdict:<9}  {src:<34}  {q}")
    return values


if __name__ == "__main__":
    print(f"Current MAX_DISTANCE = {MAX_DISTANCE}")
    ins = run("SHOULD ANSWER", IN_KB)
    outs = run("SHOULD NOT ANSWER", OUT_KB)

    worst_in, best_out = max(ins), min(outs)
    print(f"\nFarthest real question : {worst_in:.3f}")
    print(f"Closest unrelated one  : {best_out:.3f}")
    if worst_in < best_out:
        print(f"Suggested MAX_DISTANCE : {(worst_in + best_out) / 2:.2f}")
    else:
        print("The ranges overlap. Add more 'Common problems' phrases to the docs, then re-ingest.")