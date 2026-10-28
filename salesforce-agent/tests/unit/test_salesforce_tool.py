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

"""Offline tests for the Auth Manager-backed Salesforce 2LO tool and client."""

import io
from unittest.mock import MagicMock

import pytest
import requests
from docx import Document
from google.adk.auth.auth_credential import AuthCredential, OAuth2Auth

from app.agent import search_salesforce
from app.salesforce_client import (
    SalesforceClient,
    _build_excerpt,
    _build_sosl,
    _escape_sosl,
    _extract_text,
    _file_name,
)
from app.tools import _extract_token


def _make_oauth_credential(token: str = "app-token") -> AuthCredential:
    """Builds an OAuth2 AuthCredential matching Auth Manager's 2LO output."""
    return AuthCredential(
        auth_type="oauth2",
        oauth2=OAuth2Auth(access_token=token),
    )


# --- Tool & Token Extraction behavior ------------------------------------------


def test_extract_token_from_oauth2_credential() -> None:
    cred = _make_oauth_credential("my-2lo-token")
    assert _extract_token(cred) == "my-2lo-token"


def test_extract_token_returns_none_when_empty() -> None:
    assert _extract_token(None) is None


def test_search_salesforce_success(mocker) -> None:
    """Happy path: Auth Manager injects AuthCredential; client searches Files."""
    mock_search = mocker.patch(
        "app.salesforce_client.SalesforceClient.search_files",
        return_value=[
            {
                "name": "adr-014-async-timeouts.docx",
                "url": "https://test.my.salesforce.com/lightning/r/ContentDocument/069x/view",
                "snippet": "Outbound calls must set a timeout.",
            }
        ],
    )

    cred = _make_oauth_credential("app-token")
    result = search_salesforce.func("timeout", cred)

    assert result["status"] == "success"
    assert result["data"][0]["name"] == "adr-014-async-timeouts.docx"
    assert result["data"][0]["snippet"] == "Outbound calls must set a timeout."
    mock_search.assert_called_once_with("app-token", "timeout")


def test_search_returns_error_when_credential_missing() -> None:
    """Missing token from Auth Manager returns a structured error dict."""
    result = search_salesforce.func("timeout", None)
    assert result["status"] == "error"
    assert "no token from auth manager" in result["error_message"].lower()


def test_search_returns_error_dict_on_client_failure(mocker) -> None:
    """Client HTTP/search errors are surfaced as a structured error dict."""
    mocker.patch(
        "app.salesforce_client.SalesforceClient.search_files",
        side_effect=RuntimeError("SOSL failure"),
    )
    cred = _make_oauth_credential("app-token")
    result = search_salesforce.func("timeout", cred)
    assert result["status"] == "error"
    assert "search failed" in result["error_message"].lower()


# --- Client request layer (credential-free) ------------------------------------


def test_client_derives_instance_url_from_my_domain() -> None:
    client = SalesforceClient("https://myorg.my.salesforce.com/")
    assert client.domain == "myorg.my.salesforce.com"
    assert client.instance_url == "https://myorg.my.salesforce.com"


def test_search_files_maps_records_and_enriches_top_hits(mocker) -> None:
    """search_files issues a SOSL GET and enriches hits with a downloaded excerpt."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "searchRecords": [
            {
                "Id": "068A",
                "Title": "adr-014-async-timeouts",
                "FileExtension": "docx",
                "ContentDocumentId": "069A",
            }
        ]
    }
    get = mocker.patch("app.salesforce_client.requests.get", return_value=resp)
    mocker.patch(
        "app.salesforce_client.SalesforceClient.download_version_text",
        return_value=("filler " * 40) + " NEEDLE timeout rule " + ("tail " * 40),
    )

    client = SalesforceClient("org.my.salesforce.com")
    docs = client.search_files("tok", "NEEDLE")

    assert len(docs) == 1
    assert docs[0]["name"] == "adr-014-async-timeouts.docx"
    assert docs[0]["url"] == (
        "https://org.my.salesforce.com/lightning/r/ContentDocument/069A/view"
    )
    assert "NEEDLE" in docs[0]["snippet"]
    url = get.call_args[0][0]
    kwargs = get.call_args[1]
    assert url == "https://org.my.salesforce.com/services/data/v62.0/search"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["params"]["q"].startswith("FIND {NEEDLE} IN ALL FIELDS")
    assert "RETURNING ContentVersion(" in kwargs["params"]["q"]


def test_search_files_enrichment_failure_leaves_empty_snippet(mocker) -> None:
    """A download/parse failure leaves an empty snippet (title still returned)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "searchRecords": [
            {
                "Id": "068A",
                "Title": "doc",
                "FileExtension": "docx",
                "ContentDocumentId": "069A",
            }
        ]
    }
    mocker.patch("app.salesforce_client.requests.get", return_value=resp)
    mocker.patch(
        "app.salesforce_client.SalesforceClient.download_version_text",
        side_effect=RuntimeError("download boom"),
    )

    client = SalesforceClient("org.my.salesforce.com")
    docs = client.search_files("tok", "q")

    assert docs[0]["name"] == "doc.docx"
    assert docs[0]["snippet"] == ""


def test_download_version_text_extracts(mocker) -> None:
    """download_version_text fetches VersionData bytes and extracts text."""
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"plain body text"
    get = mocker.patch("app.salesforce_client.requests.get", return_value=resp)

    client = SalesforceClient("org.my.salesforce.com")
    text = client.download_version_text("tok", "068A", "notes.txt")

    assert text == "plain body text"
    url = get.call_args[0][0]
    assert url.endswith("/services/data/v62.0/sobjects/ContentVersion/068A/VersionData")


def test_search_files_raises_includes_body(mocker) -> None:
    """A >=400 search response raises HTTPError carrying the Salesforce error body."""
    resp = MagicMock()
    resp.status_code = 400
    resp.url = "https://org.my.salesforce.com/services/data/v62.0/search"
    resp.text = '[{"message":"MALFORMED_SEARCH"}]'
    mocker.patch("app.salesforce_client.requests.get", return_value=resp)

    client = SalesforceClient("org.my.salesforce.com")
    with pytest.raises(requests.HTTPError) as exc:
        client.search_files("tok", "q")
    assert "MALFORMED_SEARCH" in str(exc.value)


# --- Helpers -------------------------------------------------------------------


def test_build_sosl_targets_latest_content_versions() -> None:
    sosl = _build_sosl("async timeout")
    assert sosl.startswith(
        "FIND {async timeout} IN ALL FIELDS RETURNING ContentVersion("
    )
    assert "IsLatest = true" in sosl
    assert "ContentDocumentId" in sosl


def test_escape_sosl_escapes_reserved_chars() -> None:
    assert _escape_sosl("a+b (c)") == r"a\+b \(c\)"


def test_file_name_appends_extension() -> None:
    assert _file_name("adr-014", "docx") == "adr-014.docx"
    assert _file_name("notes.txt", "txt") == "notes.txt"
    assert _file_name("", "") == "document"


def test_extract_text_txt_md_and_unknown() -> None:
    assert _extract_text("notes.txt", b"hello world") == "hello world"
    assert _extract_text("readme.md", b"# Title") == "# Title"
    assert _extract_text("image.png", b"\x89PNG") == ""


def test_extract_text_docx_roundtrip() -> None:
    """A real .docx round-trips through python-docx extraction."""
    doc = Document()
    doc.add_paragraph("mTLS between services is mandatory")
    buf = io.BytesIO()
    doc.save(buf)
    text = _extract_text("adr.docx", buf.getvalue())
    assert "mTLS between services is mandatory" in text


def test_build_excerpt_windows_around_match() -> None:
    text = ("a " * 2000) + "NEEDLE marker here " + ("b " * 2000)
    excerpt = _build_excerpt(text, "NEEDLE", max_chars=200)
    assert "NEEDLE" in excerpt
    assert excerpt.startswith("…") and excerpt.endswith("…")
    assert len(excerpt) <= 202


def test_build_excerpt_leading_when_no_match() -> None:
    text = "alpha beta gamma delta " * 100
    excerpt = _build_excerpt(text, "zzz-not-present", max_chars=50)
    assert excerpt.startswith("alpha beta")
    assert excerpt.endswith("…")


def test_build_excerpt_short_text_returned_whole() -> None:
    assert _build_excerpt("tiny doc body", "doc") == "tiny doc body"
