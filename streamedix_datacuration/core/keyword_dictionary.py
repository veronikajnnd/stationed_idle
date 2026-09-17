"""
故障実績分類 共通辞書のローダー (ADR-2026-06-16 決定事項7)
Shared keyword-dictionary loader for failure classification (ADR-2026-06-16 decision 7).

実際の配置先 / Real destination in the repo:
    streamedix_datacuration/core/keyword_dictionary.py
    (failure_classifier.py と同じディレクトリ / same directory as failure_classifier.py)

failure_classifier.py と classification_report.py はどちらもこのモジュール経由で
config/keywords.yaml を読み込む。ロードは1回だけ行い
(load_dictionary は lru_cache でメモ化)、内容は起動時に検証する。壊れた YAML
(必須フィールド欠落・不正な classification 値・重複語 等) は import 時点で
DictionaryValidationError を送出し、壊れた辞書のまま本番が動き続けることを防ぐ。

Both failure_classifier.py and classification_report.py load
config/keywords.yaml through this module. Loading happens once
(load_dictionary is memoized via lru_cache) and the content is validated at load time.
A malformed YAML file (missing required field, invalid classification value, duplicate
word, etc.) raises DictionaryValidationError at import time, so a broken dictionary
fails fast instead of silently misclassifying rows in production.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import yaml

# failure_classifier.py の VALID_CLASSIFICATIONS と同じ値。循環importを避けるためここでも
# 定義する (どちらも唯一の正本である ADR-2026-06-16 の4分類に対応、値がずれることはない)。
# Same values as failure_classifier.py's VALID_CLASSIFICATIONS. Redefined here to avoid a
# circular import; both correspond to the single source of truth (ADR-2026-06-16's 4
# classifications), so they can't drift apart in practice.
VALID_CLASSIFICATIONS = ("failure", "inspection", "maintenance", "no_fault")

# config/keywords.yaml (streamedix_datacuration/core/ から見てリポジトリルート直下の
# config/ 配下)。
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "keywords.yaml"


class DictionaryValidationError(ValueError):
    """keywords.yaml の内容が不正な場合に送出する。起動時に fail-fast させるための専用例外。

    Raised when keywords.yaml's content is invalid. A dedicated exception (rather than a
    bare ValueError) so callers can catch validation failures specifically, e.g. to fail a
    deploy health check without swallowing unrelated ValueErrors.
    """


@dataclass(frozen=True)
class RuleException:
    """ルール2.5の例外エントリ1件 (NFKC正規化済みの word を保持)。

    One rule2.5 exception entry (word is already NFKC-normalized)."""

    word: str
    classification: str
    added: str
    reason: str


@dataclass(frozen=True)
class KeywordDictionary:
    """検証済みの辞書。全キーワードは NFKC 正規化済み (failure_regex を除き compare 対象外)。

    Validated dictionary. Every keyword field is already NFKC-normalized."""

    version: int
    updated: str
    failure_keywords: List[str]
    negation_pattern: str
    canonical_tokens: Dict[str, str]
    inspection_keyword: str
    exceptions: List[RuleException]
    maintenance_keywords: List[str]
    no_fault_category_keywords: List[str]
    repair_keywords: List[str]
    # 事前コンパイル済みの rule1 正規表現。dataclass の等価比較からは除外
    # (Pattern オブジェクトの比較は意味を持たないため)。
    # Pre-compiled rule1 regex. Excluded from dataclass equality (comparing Pattern objects
    # isn't meaningful).
    failure_regex: "re.Pattern[str]" = field(compare=False)


def normalize(text: str) -> str:
    """マッチング前処理: Unicode NFKC 正規化 (半角カナ→全角、全角英数→半角 等を統一)。

    区分+ノートの本文にも、辞書からロードした全キーワードにも同じ関数を通す。どちらか
    片方だけ正規化すると幅の違いで一致しなくなるため、呼び出し側は必ず両方に適用すること。

    Pre-matching normalization: Unicode NFKC (unifies e.g. half-width katakana to
    full-width, full-width alphanumerics to half-width).

    Apply this to BOTH the category+note text and every dictionary keyword — normalizing
    only one side reintroduces width mismatches, which defeats the point.
    """
    return unicodedata.normalize("NFKC", text)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DictionaryValidationError(message)


def _validate_classification(value: object, where: str) -> None:
    _require(
        value in VALID_CLASSIFICATIONS,
        f"{where}: unknown classification {value!r}, must be one of {VALID_CLASSIFICATIONS}",
    )


def _normalized_list(raw_list: object, where: str) -> List[str]:
    _require(isinstance(raw_list, list) and len(raw_list) > 0, f"{where} must be a non-empty list")
    normalized = [normalize(str(w)) for w in raw_list]
    _require(
        len(normalized) == len(set(normalized)),
        f"{where} has duplicate entries after NFKC normalization: "
        f"{[w for w in normalized if normalized.count(w) > 1]}",
    )
    return normalized


def _validate_and_build(raw: object) -> KeywordDictionary:
    _require(isinstance(raw, dict), "keywords.yaml top level must be a mapping")
    _require("version" in raw, "keywords.yaml is missing required field: version")
    _require(isinstance(raw["version"], int), "keywords.yaml: version must be an integer")

    required_sections = (
        "failure_signal", "canonical_tokens", "inspection",
        "exceptions", "maintenance", "no_fault", "repair",
    )
    for section in required_sections:
        _require(section in raw, f"keywords.yaml is missing required section: {section}")

    failure_signal = raw["failure_signal"]
    _require(isinstance(failure_signal, dict), "failure_signal must be a mapping")
    _require("keywords" in failure_signal, "failure_signal is missing required field: keywords")
    _require(
        "negation_pattern" in failure_signal,
        "failure_signal is missing required field: negation_pattern",
    )
    failure_keywords = _normalized_list(failure_signal["keywords"], "failure_signal.keywords")
    negation_pattern = failure_signal["negation_pattern"]
    _require(isinstance(negation_pattern, str) and negation_pattern, "failure_signal.negation_pattern must be a non-empty string")

    canonical_tokens_raw = raw["canonical_tokens"]
    _require(isinstance(canonical_tokens_raw, dict) and canonical_tokens_raw, "canonical_tokens must be a non-empty mapping")
    canonical_tokens: Dict[str, str] = {}
    for token, cls in canonical_tokens_raw.items():
        _validate_classification(cls, f"canonical_tokens[{token!r}]")
        canonical_tokens[str(token)] = cls

    inspection_raw = raw["inspection"]
    _require(isinstance(inspection_raw, dict) and "keyword" in inspection_raw, "inspection is missing required field: keyword")
    inspection_keyword = normalize(str(inspection_raw["keyword"]))

    exceptions_raw = raw["exceptions"] or []
    _require(isinstance(exceptions_raw, list), "exceptions must be a list")
    exceptions: List[RuleException] = []
    seen_words = set()
    for i, exc in enumerate(exceptions_raw):
        _require(isinstance(exc, dict), f"exceptions[{i}] must be a mapping")
        for req_field in ("word", "classification", "added", "reason"):
            _require(req_field in exc, f"exceptions[{i}] is missing required field: {req_field}")
        word = normalize(str(exc["word"]))
        _validate_classification(exc["classification"], f"exceptions[{i}] (word={word!r})")
        _require(word not in seen_words, f"exceptions has a duplicate word (after NFKC normalization): {word!r}")
        seen_words.add(word)
        exceptions.append(
            RuleException(
                word=word,
                classification=exc["classification"],
                added=str(exc["added"]),
                reason=str(exc["reason"]),
            )
        )

    maintenance_raw = raw["maintenance"]
    _require(isinstance(maintenance_raw, dict) and "keywords" in maintenance_raw, "maintenance is missing required field: keywords")
    maintenance_keywords = _normalized_list(maintenance_raw["keywords"], "maintenance.keywords")

    no_fault_raw = raw["no_fault"]
    _require(isinstance(no_fault_raw, dict) and "category_keywords" in no_fault_raw, "no_fault is missing required field: category_keywords")
    no_fault_category_keywords = _normalized_list(no_fault_raw["category_keywords"], "no_fault.category_keywords")

    repair_raw = raw["repair"]
    _require(isinstance(repair_raw, dict) and "keywords" in repair_raw, "repair is missing required field: keywords")
    repair_keywords = _normalized_list(repair_raw["keywords"], "repair.keywords")

    # rule1 の否定除外つき正規表現をロード時に1回だけコンパイルする (毎回コンパイルする
    # コストを避けるため、failure_classifier.py 側の挙動を維持)。
    # Compile rule1's negation-excluding regex once at load time (avoids recompiling on
    # every call; matches failure_classifier.py's existing behavior).
    failure_regex = re.compile(
        r"(?:%s)(?!%s)" % ("|".join(re.escape(w) for w in failure_keywords), negation_pattern),
        re.IGNORECASE,
    )

    return KeywordDictionary(
        version=raw["version"],
        updated=str(raw.get("updated", "")),
        failure_keywords=failure_keywords,
        negation_pattern=negation_pattern,
        canonical_tokens=canonical_tokens,
        inspection_keyword=inspection_keyword,
        exceptions=exceptions,
        maintenance_keywords=maintenance_keywords,
        no_fault_category_keywords=no_fault_category_keywords,
        repair_keywords=repair_keywords,
        failure_regex=failure_regex,
    )


@lru_cache(maxsize=None)
def load_dictionary(path: Optional[str] = None) -> KeywordDictionary:
    """keywords.yaml をロード・検証し、キャッシュして返す。

    同じ path での2回目以降の呼び出しは再パースせずキャッシュを返す (failure_classifier.py
    と classification_report.py が両方 import してもファイル I/O は1回だけで済む)。

    テストで異なる内容を検証する場合は load_dictionary.cache_clear() を呼んでから
    別の path (一時ファイル) を渡すこと。path=None (デフォルト) は本番の
    config/keywords.yaml を指す。

    Loads, validates and caches keywords.yaml. Repeated calls with the same path return
    the cached result without re-parsing, so failure_classifier.py and
    classification_report.py importing this share a single file read.

    Tests that need different content should call load_dictionary.cache_clear() first and
    pass a different path (a temp file). path=None (the default) points at the real
    config/keywords.yaml.
    """
    target = Path(path) if path else _DEFAULT_PATH
    with target.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return _validate_and_build(raw)