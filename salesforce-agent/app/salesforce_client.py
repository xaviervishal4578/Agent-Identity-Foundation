# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Salesforce Files connector (2-legged OAuth / client credentials).

``authenticate`` performs the client-credentials grant (app identity, no user);
``search_files`` then runs a full-text search over **Salesforce Files** (the
``ContentVersion`` object) with the resulting app token, so results span every
file the app can read rather than a single record's attachments. Salesforce
full-text-indexes uploaded ``.docx`` / ``.pdf`` / ``.pptx`` / text file content,
which is what makes drag-and-drop documents searchable.

Salesforce Object Search Language (SOSL) returns matching file versions but no
usable content snippet, so the top results are enriched: the file's bytes are
downloaded via the ``ContentVersion`` ``VersionData`` resource and a plain-text
excerpt is built around the matched query term (``.docx``/``.pdf``/``.txt``/
``.md`` supported).

Unlike Microsoft Graph, Salesforce does not return ``expires_in`` for this flow
(the access token is a session id whose lifetime is governed by the org's session
policy). The caller caches the token with a conservative TTL and re-authenticates
on a ``401``.

Bundled inside ``app/`` so it ships in the Cloud Run container image alongside
the agent.
"""

import io
import re

import requests
from pypdf import PdfReader

from docx import Document

# Default REST API version used to build the ``/services/data/vXX.X/`` paths.
_DEFAULT_API_VERSION = "v62.0"
_TIMEOUT_SECONDS = 10
# Downloads (file content) can be larger/slower than metadata calls.
_CONTENT_TIMEOUT_SECONDS = 30
# Default page size for search results.
_SEARCH_PAGE_SIZE = 25
# Only the top-K hits are downloaded + excerpted (bounds download cost/latency).
_ENRICH_TOP_K = 5
# Target length for the content excerpt used as a search snippet.
_EXCERPT_MAX_CHARS = 1500


def _extract_text(name: str, content: bytes) -> str:
    """Extracts plain text from downloaded file bytes, by file extension.

    Supports ``.docx``, ``.pdf``, ``.txt`` and ``.md``; returns ``""`` for
    anything else (the caller then falls back to an empty snippet).
    """
    lower = name.lower()
    if lower.endswith(".docx"):
        document = Document(io.BytesIO(content))
        return "\n".join(p.text for p in document.paragraphs)
    if lower.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if lower.endswith((".txt", ".md")):
        return content.decode("utf-8", errors="ignore")
    return ""


def _build_excerpt(text: str, query: str, max_chars: int = _EXCERPT_MAX_CHARS) -> str:
    """Builds an excerpt of ``text`` centered on the first query-term match.

    Tries the full query first, then any whitespace-split term. When nothing
    matches, returns the leading ``max_chars``. Whitespace is collapsed and the
    excerpt is bracketed with ellipses when it is a middle slice.
    """
    text = " ".join(text.split())
    if not text:
        return ""

    terms = [query, *query.split()]
    lowered = text.lower()
    idx = -1
    for term in terms:
        term = term.strip().lower()
        if term:
            idx = lowered.find(term)
            if idx != -1:
                break

    if idx == -1 or len(text) <= max_chars:
        excerpt = text[:max_chars]
        return excerpt + ("…" if len(text) > max_chars else "")

    start = max(0, idx - max_chars // 2)
    end = min(len(text), start + max_chars)
    excerpt = text[start:end]
    return ("…" if start > 0 else "") + excerpt + ("…" if end < len(text) else "")


def _escape_sosl(term: str) -> str:
    """Escapes SOSL reserved characters in a user-supplied search term.

    SOSL treats ``? & | ! { } [ ] ( ) ^ ~ * : \\ " ' + -`` as reserved; each must
    be backslash-escaped inside the ``FIND {...}`` clause.
    """
    return re.sub(r'([?&|!{}\[\]()^~*:\\"\'+\-])', r"\\\1", term)


def _build_sosl(query: str) -> str:
    """Builds a SOSL query returning the latest version of matching files.

    ``IN ALL FIELDS`` covers the indexed file content (plus Title); the
    ``RETURNING`` filter restricts hits to the current version of each file.
    """
    returning = (
        "ContentVersion(Id, Title, FileExtension, ContentDocumentId "
        f"WHERE IsLatest = true LIMIT {_SEARCH_PAGE_SIZE})"
    )
    return f"FIND {{{_escape_sosl(query)}}} IN ALL FIELDS RETURNING {returning}"


def _file_name(title: str, extension: str) -> str:
    """Builds a file name (with extension) used to pick a text extractor."""
    title = title or "document"
    if extension and not title.lower().endswith(f".{extension.lower()}"):
        return f"{title}.{extension}"
    return title


def _raise_for_status(resp: requests.Response) -> None:
    """Like ``resp.raise_for_status()`` but includes the Salesforce error body.

    Salesforce returns actionable detail in the response body (e.g. a malformed
    SOSL clause or a permission problem); the default ``raise_for_status`` shows
    only the status line.
    """
    if resp.status_code >= 400:
        raise requests.HTTPError(
            f"{resp.status_code} for {resp.url}: {resp.text}", response=resp
        )


class SalesforceClient:
    """auth provider to search Salesforce Files (no credentials of its own).

    Holds only the org's My Domain and REST API version. The app access token is
    resolved by Auth Manager and passed in per call; the REST instance URL is
    derived from the My Domain (``https://< domain>``).
    """

    def __init__(
        self,
        domain: str,
        api_version: str | None = None,
    ):
        # Accept either a bare host or a full URL for the org's My Domain.
        self.domain = domain.replace("https://", "").replace("http://", "").strip("/")
        self.api_version = api_version or _DEFAULT_API_VERSION
        # With My Domain / Enhanced Domains, the My Domain host is a valid API
        # instance URL. Auth Manager does not surface Salesforce's instance_url,
        # so the REST base is derived here instead of from the token response.
        self.instance_url = f"https://{self.domain}"

    def download_version_text(
        self, access_token: str, version_id: str, name: str
    ) -> str:
        """Downloads a file version's bytes and extracts plain text.

        Args:
            access_token: An app access token resolved by Auth Manager.
            version_id: The ``ContentVersion`` id.
            name: The file name (its extension selects the text extractor).

        Returns:
            The extracted text, or ``""`` for unsupported file types.
        """
        resp = requests.get(
            f"{self.instance_url}/services/data/{self.api_version}"
            f"/sobjects/ContentVersion/{version_id}/VersionData",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_CONTENT_TIMEOUT_SECONDS,
        )
        _raise_for_status(resp)
        return _extract_text(name, resp.content)

    def search_files(self, access_token: str, query: str) -> list:
        """Searches Salesforce Files (ContentVersion) via SOSL full-text search.

        Uses ``GET /services/data/vXX.X/search`` with a SOSL ``FIND`` so it spans
        every file the app can read, restricted to the latest version of each.
        The top ``_ENRICH_TOP_K`` hits are enriched with a text excerpt built from
        the downloaded file bytes (SOSL itself returns no content snippet).

        Args:
            access_token: An app access token resolved by Auth Manager.
            query: The user's search terms.

        Returns:
            A list of ``{'name', 'url', 'snippet'}`` dicts for matching files.
        """
        resp = requests.get(
            f"{self.instance_url}/services/data/{self.api_version}/search",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": _build_sosl(query)},
            timeout=_TIMEOUT_SECONDS,
        )
        _raise_for_status(resp)

        documents = []
        for i, record in enumerate(resp.json().get("searchRecords", [])):
            version_id = record.get("Id")
            document_id = record.get("ContentDocumentId")
            name = _file_name(record.get("Title"), record.get("FileExtension"))
            snippet = ""
            # Only download + excerpt the top hits to bound latency/cost.
            if i < _ENRICH_TOP_K and version_id:
                try:
                    text = self.download_version_text(access_token, version_id, name)
                    snippet = _build_excerpt(text, query)
                except Exception:
                    # Enrichment is best-effort; a download/parse failure just
                    # leaves an empty snippet (the title still helps the model).
                    snippet = ""
            documents.append(
                {
                    "name": name,
                    "url": (
                        f"{self.instance_url}/lightning/r/ContentDocument/{document_id}/view"
                        if document_id
                        else self.instance_url
                    ),
                    "snippet": snippet,
                }
            )
        return documents
