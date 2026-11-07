"""Synthetic, labelled LLM query stream for evaluating cache reuse decisions.

Every query carries a ground-truth ``key`` = (intent family, slot values). Two queries may share an answer
iff their keys are equal and the family is not volatile (time-sensitive). Families are grouped into
*topics*; sibling families inside a topic are deliberate near-misses (enable vs disable, read vs write,
password vs username), and slot changes inside a family are near-misses of another kind (order 4521 vs 4512).
"""
from __future__ import annotations

import itertools
import json
import random
from dataclasses import dataclass

ORDERS = ["4521", "4512", "88310", "88301", "10027", "10072", "55120", "73419"]
PLANS = ["free", "basic", "pro", "enterprise"]
LANGS = ["python", "java", "javascript", "rust", "golang"]
FORMATS = ["csv", "json", "excel", "pdf"]
CITIES = ["london", "paris", "tokyo", "berlin", "mumbai", "toronto"]
ERRORS = ["401", "403", "404", "429", "500", "503"]
CURRENCIES = ["usd", "eur", "gbp", "inr", "jpy"]
DAYS = ["3", "5", "7", "10", "14"]
# surface forms: the key uses the canonical value, the text may spell it out
SURFACE = {"3": ["3", "three"], "5": ["5", "five"], "10": ["10", "ten"]}
HOURS = ["4", "6", "8", "10"]


@dataclass(frozen=True)
class Family:
    id: str
    topic: str
    templates: tuple[str, ...]
    slots: dict
    volatile: bool = False
    contrast: str = ""          # how it differs from its siblings: polarity / object / scope


def F(id, topic, templates, slots=None, volatile=False, contrast=""):
    return Family(id, topic, tuple(templates), slots or {}, volatile, contrast)


FAMILIES: list[Family] = [
    # --- topic: two-factor auth (polarity siblings)
    F("enable_2fa", "auth_2fa", [
        "how do I enable two-factor authentication", "turn on 2fa for my account",
        "how can I activate two factor auth", "where do I switch on 2-step verification",
        "steps to enable 2fa", "I want to turn on two-factor authentication on my account"], contrast="polarity"),
    F("disable_2fa", "auth_2fa", [
        "how do I disable two-factor authentication", "turn off 2fa for my account",
        "how can I deactivate two factor auth", "where do I switch off 2-step verification",
        "steps to disable 2fa", "I want to turn off two-factor authentication on my account"], contrast="polarity"),
    # --- topic: credentials (object siblings)
    F("reset_password", "credentials", [
        "how do I reset my password", "I forgot my password, how can I reset it", "password reset steps",
        "how to reset the password for my login", "help me reset my password please",
        "what's the process to reset a forgotten password"], contrast="object"),
    F("change_username", "credentials", [
        "how do I change my username", "I want a different username, how can I change it", "username change steps",
        "how to change the username for my login", "help me change my username please",
        "what's the process to rename my username"], contrast="object"),
    F("change_email", "credentials", [
        "how do I change my email address", "I want a different email on my account, how can I change it",
        "email address change steps", "how to update the email for my login", "help me change my email please",
        "what's the process to switch my account email"], contrast="object"),
    F("cannot_login", "credentials", [
        "why can't I log in to my account", "I cannot log into my account", "login is not working for me",
        "unable to sign in to my account", "I can't sign in anymore, what's wrong"], contrast="negation"),
    F("how_login", "credentials", [
        "how do I log in to my account", "where do I sign in to my account", "how to log into my account",
        "what's the way to sign in", "I want to sign in to my account, how"], contrast="negation"),
    # --- topic: orders (slot near-misses, one volatile family)
    F("refund_order", "orders", [
        "can I get a refund for order {order}", "I want my money back for order #{order}",
        "refund request for order {order}", "how do I return order {order} for a refund",
        "please refund order no. {order}", "order {order} refund please"], {"order": ORDERS}, contrast="slot"),
    F("order_status", "orders", [
        "what is the status of order {order} right now", "track order {order} today",
        "where is order {order} currently", "latest update on order #{order}",
        "is order {order} shipped yet? need the current status"], {"order": ORDERS}, volatile=True),
    # --- topic: billing plans
    F("plan_price", "billing", [
        "how much does the {plan} plan cost", "what's the price of the {plan} plan", "{plan} plan pricing",
        "monthly cost for the {plan} tier", "how expensive is the {plan} subscription",
        "price of {plan} plan per month"], {"plan": PLANS}, contrast="slot"),
    F("upgrade_plan", "billing", [
        "how do I upgrade to the {plan} plan", "upgrade my subscription to {plan}",
        "move me up to the {plan} tier", "I'd like to upgrade to {plan}", "steps to upgrade my account to {plan}"],
      {"plan": PLANS}, contrast="polarity"),
    F("downgrade_plan", "billing", [
        "how do I downgrade to the {plan} plan", "downgrade my subscription to {plan}",
        "move me down to the {plan} tier", "I'd like to downgrade to {plan}", "steps to downgrade my account to {plan}"],
      {"plan": PLANS}, contrast="polarity"),
    F("balance_due", "billing", [
        "what is my current balance", "how much do I owe right now", "amount due on my account today",
        "show my latest invoice balance"], volatile=True),
    # --- topic: coding help
    F("read_csv", "coding", [
        "how do I read a csv file in {lang}", "{lang} code to load a csv", "parse a csv file using {lang}",
        "what's the easiest way to read a csv in {lang}", "reading csv files with {lang}"],
      {"lang": LANGS}, contrast="polarity"),
    F("write_csv", "coding", [
        "how do I write a csv file in {lang}", "{lang} code to save a csv", "write rows to a csv file using {lang}",
        "what's the easiest way to write a csv in {lang}", "writing csv files with {lang}"],
      {"lang": LANGS}, contrast="polarity"),
    F("sort_desc", "coding", [
        "how to sort a list in descending order in {lang}", "{lang} sort array descending",
        "sort a list from largest to smallest in {lang}", "reverse sort a list in {lang}",
        "descending sort of a list using {lang}"], {"lang": LANGS}, contrast="polarity"),
    F("sort_asc", "coding", [
        "how to sort a list in ascending order in {lang}", "{lang} sort array ascending",
        "sort a list from smallest to largest in {lang}", "sort a list in increasing order in {lang}",
        "ascending sort of a list using {lang}"], {"lang": LANGS}, contrast="polarity"),
    # --- topic: leave policy
    F("leave_days", "leave", [
        "can I take {n} days of vacation in a row", "is it ok to book {n} consecutive days off",
        "am I allowed {n} days of leave at once", "request for {n} vacation days back to back, is that allowed",
        "can I go on holiday for {n} days straight"], {"n": DAYS}, contrast="slot"),
    F("leave_carryover", "leave", [
        "can unused vacation days carry over to next year", "do leftover pto days roll over",
        "what happens to vacation days I don't use", "is there a carry-over limit for annual leave",
        "can I carry forward unused leave"], contrast="object"),
    # --- topic: travel expenses
    F("per_diem", "travel", [
        "what is the per diem for {city}", "daily meal allowance when travelling to {city}",
        "how much can I expense per day in {city}", "{city} per diem rate", "travel allowance per day for {city}"],
      {"city": CITIES}, contrast="slot"),
    F("flight_class", "travel", [
        "can I book business class for a {h} hour flight", "is business class allowed on a {h} hour flight",
        "flight of {h} hours: economy or business?", "business class policy for {h}-hour flights",
        "do we fly business on {h} hour trips"], {"h": HOURS}, contrast="slot"),
    F("flight_delay", "travel", [
        "is flight {fl} delayed right now", "current status of flight {fl}", "is {fl} on time today",
        "latest departure time for flight {fl}"], {"fl": ["ba117", "lh400", "ua901", "af22"]}, volatile=True),
    # --- topic: live data (volatile)
    F("fx_rate", "live_data", [
        "what is the {a} to {b} exchange rate today", "current {a} to {b} rate",
        "how many {b} is one {a} right now", "latest {a}/{b} conversion rate"],
      {"a": CURRENCIES[:3], "b": CURRENCIES[2:]}, volatile=True),
    F("weather", "live_data", [
        "what's the weather in {city} today", "is it raining in {city} right now",
        "current temperature in {city}", "{city} weather forecast for tonight"], {"city": CITIES}, volatile=True),
    # --- topic: api errors
    F("error_code", "api", [
        "what does error {code} mean", "I'm getting error code {code} from the api", "how to fix error {code}",
        "{code} error when calling the api", "api returns {code}, what should I do"], {"code": ERRORS}, contrast="slot"),
    F("rate_limit", "api", [
        "what is the api rate limit on the {plan} plan", "how many requests per minute does the {plan} plan allow",
        "api quota for {plan} accounts", "{plan} plan api request limit", "rate limits for the {plan} tier api"],
      {"plan": PLANS}, contrast="slot"),
    F("api_status", "api", [
        "is the api down right now", "current api status", "are there any api outages today",
        "latest api incident status"], volatile=True),
    # --- topic: data portability
    F("export_data", "data", [
        "how do I export my data as {fmt}", "export all my data in {fmt} format", "can I get a {fmt} export of my account",
        "export account data to {fmt}", "is there a way to export everything as {fmt}"], {"fmt": FORMATS},
      contrast="polarity"),
    F("import_data", "data", [
        "how do I import my data from {fmt}", "import all my data in {fmt} format", "can I do a {fmt} import into my account",
        "import account data from {fmt}", "is there a way to import everything from {fmt}"], {"fmt": FORMATS},
      contrast="polarity"),
    # --- topic: account security
    F("lock_account", "security", [
        "how do I lock my account if my phone was stolen", "freeze my account immediately",
        "temporarily lock my account", "lock my account, I think it was hacked", "how can I freeze my account"],
      contrast="polarity"),
    F("unlock_account", "security", [
        "how do I unlock my account", "my account got locked, how do I unlock it", "unfreeze my account",
        "unlock my account, I was locked out", "how can I unfreeze my account"], contrast="polarity"),
    F("delete_account", "security", [
        "how do I permanently delete my account", "close my account for good",
        "delete my account and all my data", "I want to delete my account permanently",
        "how can I get my account deleted"], contrast="object"),
]

FILLERS = ["hey, ", "quick question: ", "hi team, ", "please help: ", "hello! "]
SUFFIXES = ["?", "??", " thanks", " asap", ".", " please"]


def _typo(text: str, rng: random.Random, protected: set[str]) -> str:
    words = text.split(" ")
    cands = [i for i, w in enumerate(words) if len(w) >= 5 and w.isalpha() and w.lower() not in protected]
    if not cands:
        return text
    i = rng.choice(cands)
    w = words[i]
    j = rng.randrange(1, len(w) - 2)
    words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2:]
    return " ".join(words)


def generate(n: int = 3000, seed: int = 7, max_keys_per_family: int = 6, zipf_s: float = 1.05) -> list[dict]:
    rng = random.Random(seed)
    keys = []
    for fam in FAMILIES:
        names = sorted(fam.slots)
        combos = [dict(zip(names, vals)) for vals in itertools.product(*(fam.slots[k] for k in names))]
        combos = [c for c in combos if not ("a" in c and c["a"] == c["b"])]
        rng.shuffle(combos)
        for c in combos[:max_keys_per_family] if combos else [{}]:
            keys.append((fam, c))
    # popularity: topics equally likely, families equally likely within a topic, Zipf over keys in a family
    topics = sorted({f.topic for f, _ in keys})
    by_family: dict[str, list] = {}
    for fam, c in keys:
        by_family.setdefault(fam.id, []).append((fam, c))
    fam_by_topic = {t: sorted({f.id for f, _ in keys if f.topic == t}) for t in topics}
    out = []
    for i in range(n):
        fid = rng.choice(fam_by_topic[rng.choice(topics)])
        options = by_family[fid]
        weights = [1.0 / (r + 1) ** zipf_s for r in range(len(options))]
        fam, slots = rng.choices(options, weights)[0]
        surface = {k: rng.choice(SURFACE.get(v, [v])) for k, v in slots.items()}
        text = rng.choice(fam.templates).format(**surface)
        if rng.random() < 0.25:
            text = text[0].upper() + text[1:]
        if rng.random() < 0.2:
            text = rng.choice(FILLERS) + text
        if rng.random() < 0.3:
            text += rng.choice(SUFFIXES)
        if rng.random() < 0.08:
            text = _typo(text, rng, set(surface.values()))
        key = fam.id + "|" + "|".join(f"{k}={slots[k]}" for k in sorted(slots))
        out.append({"i": i, "text": text, "key": key, "family": fam.id, "topic": fam.topic,
                    "volatile": fam.volatile, "contrast": fam.contrast})
    return out


def write_jsonl(records: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
