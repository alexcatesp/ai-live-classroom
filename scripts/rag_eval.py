"""Does keyword search find the right page of the teacher's notes? (H6, D-14)

The MVP has to answer with the module's materials, and the Realtime API is too
expensive to hold them all in the prompt: a single unit is about 50,000 tokens,
re-read on every question. So each question must bring only the two or three
sections that matter -- and before building that into the application, it is
worth knowing whether plain keyword search finds them.

The measurement needs no judgement and no model, because the teacher's own
files carry the answer key: every section of the notes is tagged with the
criterion it develops, `*(CE a - IL1)*`, and every item of the exam bank is
named after the same criterion, `[RA1-a-B-01]`. So the bank is a set of real
questions with the right section already known.

    python scripts/rag_eval.py --materiales <carpeta del módulo>

It prints, for the whole bank, how often the right criterion appears in the
first result, the first three and the first five, and how long a search takes.
Nothing leaves the machine and no exam text is ever ingested: the bank is only
the ruler.
"""

import argparse
import math
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Words too common in Spanish -- and in this module -- to tell sections apart.
_STOPWORDS = """
a al algo algún alguna algunas alguno algunos ante antes aquel aquella aquellas aquellos aqui así
aun aunque bajo bien cada como con contra cual cuales cuando cuanto de del desde donde dos e el
ella ellas ello ellos en entre era eran es esa esas ese eso esos esta estan estas este esto estos
ha hace hacen hacer hacia han hasta hay la las le les lo los mas más me mi mientras misma mismo
mucha mucho muy no nos o os otra otras otro otros para pero poco por porque pues que qué quien
se ser si sin sobre son su sus también tan tanto te tiene tienen toda todas todo todos tras tu tus
un una unas uno unos ya y
ejemplo ejemplos explica indica señala describe responde pregunta respuesta criterio solución
nivel tipo test concepto desarrollo caso frase frases palabras alumno alumnos clase
"""
STOPWORDS = frozenset(_STOPWORDS.split())

# The criterion a section develops is written two ways in the teacher's files:
# "### 2.1 Hitos: una cronología mínima *(CE a · IL1)*" in the notes, and
# "## CE a — Se han identificado los hitos..." in the exercises.
CRITERION = re.compile(r"\(CE\s+([a-z])\b[^)]*\)|(?:^|\s)CE\s+([a-z])\s+[—–-]", re.IGNORECASE)


def criterion_of(title: str) -> str:
    """The criterion in a heading, or in the heading it hangs from."""
    # Read the trail right to left: the nearest heading wins.
    for part in reversed(title.split(" › ")):
        found = CRITERION.search(part)
        if found:
            return (found.group(1) or found.group(2)).lower()
    return ""


# "- **[RA1-a-B-01]** (IL1, tipo: test) Señala la única secuencia..."
ITEM = re.compile(r"\*\*\[RA1-([a-z])-([BIA])-(\d+)\]\*\*\s*(\([^)]*\))?\s*(.*)", re.DOTALL)
HEADING = re.compile(r"^(#{2,4})\s+(.*)$")
SOLUTION = re.compile(r"\*Soluci[óo]n\s*/\s*criterio:?\*", re.IGNORECASE)


def normalise(text: str) -> list[str]:
    """Words, without accents, case or endings that only add noise."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9]+", plain)
    kept = []
    for word in words:
        if word in STOPWORDS or len(word) < 3:
            continue
        # A poor stemmer on purpose: endings a question changes and a heading
        # does not -- "datos sesgados" has to reach a section about "el sesgo".
        for ending in (
            "mente", "ciones", "cion", "idades", "idad",
            "ados", "adas", "idos", "idas", "ado", "ada", "ido", "ida",
            "ando", "iendo", "aron", "aban", "amos", "an", "en",
            "es", "s",
        ):
            if len(word) > len(ending) + 3 and word.endswith(ending):
                word = word[: -len(ending)]
                break
        kept.append(word)
    return kept


@dataclass
class Chunk:
    """One section of the notes: what a question would be answered from."""

    source: str
    title: str
    criterion: str
    text: str
    words: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.source} · {self.title}" + (f" (CE {self.criterion})" if self.criterion else "")


def split_into_sections(path: Path, source: str) -> list[Chunk]:
    """Markdown cut where its own headings cut it, keeping the heading path."""
    chunks: list[Chunk] = []
    trail: dict[int, str] = {}
    title, body, criterion = source, [], ""

    def flush() -> None:
        text = "\n".join(body).strip()
        if len(text) > 200:  # a heading with nothing under it is not a section
            chunks.append(Chunk(source, title, criterion, text, normalise(title + " " + text)))

    for line in path.read_text(encoding="utf-8").splitlines():
        heading = HEADING.match(line)
        if not heading:
            body.append(line)
            continue
        flush()
        level, heading_text = len(heading.group(1)), heading.group(2).strip()
        trail[level] = re.sub(r"\*+", "", heading_text)
        for deeper in [key for key in trail if key > level]:
            del trail[deeper]
        title = " › ".join(trail[key] for key in sorted(trail))
        criterion = criterion_of(title)
        body = []
    flush()
    return chunks


@dataclass
class Question:
    identifier: str
    criterion: str
    text: str


def read_exam_items(path: Path) -> list[Question]:
    """The bank as questions, with the solutions cut off before they leak."""
    questions: list[Question] = []
    raw = path.read_text(encoding="utf-8")
    for block in re.split(r"\n(?=- \*\*\[RA1-)", raw):
        item = ITEM.search(block)
        if not item:
            continue
        statement = SOLUTION.split(item.group(5))[0]
        # Multiple-choice options describe the distractors, not the question.
        statement = re.split(r"\n\s+- [a-d]\)", statement)[0]
        statement = re.sub(r"\s+", " ", statement).strip()
        if len(statement) > 40:
            questions.append(
                Question(f"RA1-{item.group(1)}-{item.group(2)}-{item.group(3)}",
                         item.group(1).lower(), statement)
            )
    return questions


class Bm25:
    """Okapi BM25 over the sections, in sixty lines and no dependencies."""

    K1 = 1.5
    B = 0.75

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.lengths = [len(chunk.words) for chunk in chunks]
        self.average = sum(self.lengths) / max(1, len(chunks))
        self.frequencies = [Counter(chunk.words) for chunk in chunks]
        documents: Counter[str] = Counter()
        for frequency in self.frequencies:
            documents.update(frequency.keys())
        total = len(chunks)
        self.idf = {
            word: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for word, count in documents.items()
        }

    def search(self, query: str, limit: int = 5) -> list[tuple[float, Chunk]]:
        words = normalise(query)
        scores = []
        for index, frequency in enumerate(self.frequencies):
            length = self.lengths[index]
            score = 0.0
            for word in words:
                appearances = frequency.get(word, 0)
                if not appearances:
                    continue
                norm = appearances * (self.K1 + 1)
                denominator = appearances + self.K1 * (1 - self.B + self.B * length / self.average)
                score += self.idf.get(word, 0.0) * norm / denominator
            if score:
                scores.append((score, self.chunks[index]))
        scores.sort(key=lambda pair: pair[0], reverse=True)
        return scores[:limit]


INGESTED = (
    ("apuntes", "apuntes"),
    ("ejercicios", "ejercicios"),
    ("practicas", "prácticas"),
)


def gather(materials: Path) -> list[Chunk]:
    """What a class may answer from: notes, exercises and practicals.

    Never the exam bank, the model exam or the rubric: the assistant must not
    be able to read out the answers in class.
    """
    chunks: list[Chunk] = []
    for folder, label in INGESTED:
        for path in sorted((materials / folder).glob("*.md")):
            chunks.extend(split_into_sections(path, f"{label}/{path.stem}"))
    guide = materials / "guia-del-curso.md"
    if guide.exists():
        chunks.extend(split_into_sections(guide, "guía del curso"))
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(prog="rag_eval")
    parser.add_argument("--materiales", required=True, type=Path)
    parser.add_argument("--muestra", type=int, default=3, help="ejemplos que se imprimen")
    parser.add_argument(
        "--pregunta", action="append", default=[],
        help="pregunta suelta, como se diría en voz alta; repetible",
    )
    arguments = parser.parse_args()

    chunks = gather(arguments.materiales)
    index = Bm25(chunks)
    words = sum(len(chunk.words) for chunk in chunks)
    tagged = sum(1 for chunk in chunks if chunk.criterion)
    print(f"{len(chunks)} secciones · {words} palabras indexadas · {tagged} con CE")

    if arguments.pregunta:
        # Spoken questions have no answer key: they are read, not counted.
        for spoken in arguments.pregunta:
            print(f"\n> {spoken}")
            for score, chunk in index.search(spoken, limit=3):
                print(f"    {score:5.1f}  {chunk.label}")
        return

    questions = read_exam_items(arguments.materiales / "examenes" / "banco-RA1.md")
    print(f"{len(questions)} preguntas del banco de ítems (solo como medida)\n")

    hits = {1: 0, 3: 0, 5: 0}
    started = time.monotonic()
    misses = []
    for question in questions:
        results = index.search(question.text, limit=5)
        criteria = [chunk.criterion for _score, chunk in results]
        for cut in hits:
            if question.criterion in criteria[:cut]:
                hits[cut] += 1
        if not criteria[:3] or question.criterion not in criteria[:3]:
            misses.append((question, results))
    elapsed = (time.monotonic() - started) / max(1, len(questions))

    for cut in sorted(hits):
        rate = 100 * hits[cut] / max(1, len(questions))
        print(f"  el CE correcto está entre los {cut} primeros: {rate:5.1f} %")
    print(f"  {elapsed * 1000:.1f} ms por búsqueda\n")

    for question, results in misses[: arguments.muestra]:
        print(f"falla {question.identifier} (CE {question.criterion}): {question.text[:110]}…")
        for score, chunk in results[:3]:
            print(f"    {score:5.1f}  {chunk.label}")


if __name__ == "__main__":
    main()
