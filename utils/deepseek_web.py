"""Authenticated DeepSeek web client for chat, research, and vision."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit


logger = logging.getLogger("ModBot.DeepSeekWeb")

_DEEPSEEK_URL: Final = "https://chat.deepseek.com/"
_ANSWER_SELECTOR: Final = ".ds-assistant-message-main-content"
_CHALLENGE_MARKERS: Final = (
    "verify you are human",
    "checking your browser",
    "security verification",
    "turnstile",
    "captcha",
    "cf-chl",
)


class DeepSeekWebError(RuntimeError):
    """Base error raised by the DeepSeek browser provider."""


class DeepSeekWebAuthError(DeepSeekWebError):
    """The saved login expired or an interactive human check appeared."""


class DeepSeekWebClient:
    """Run isolated DeepSeek UI lanes while sharing one authenticated browser."""

    def __init__(self) -> None:
        self.enabled = os.getenv("DEEPSEEK_WEB_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.storage_state_path = Path(
            os.getenv(
                "DEEPSEEK_WEB_STORAGE_STATE",
                "/root/.config/modbot/deepseek-storage.json",
            )
        )
        self.session_index_path = Path(
            os.getenv(
                "DEEPSEEK_WEB_SESSION_INDEX",
                str(self.storage_state_path.with_name("deepseek-channel-sessions.json")),
            )
        )
        self.timeout_seconds = min(
            300,
            max(30, int(os.getenv("DEEPSEEK_WEB_TIMEOUT", "150"))),
        )
        self._start_lock = asyncio.Lock()
        self._cookie_choice_lock = asyncio.Lock()
        self._lane_locks = {
            "chat": asyncio.Lock(),
            "research": asyncio.Lock(),
            "vision": asyncio.Lock(),
        }
        self.chat_session_ttl = min(
            1_800,
            max(60, int(os.getenv("DEEPSEEK_WEB_CHAT_SESSION_TTL", "600"))),
        )
        self.max_chat_sessions = min(
            12,
            max(1, int(os.getenv("DEEPSEEK_WEB_MAX_CHAT_SESSIONS", "6"))),
        )
        self._session_last_used: dict[str, float] = {}
        self._session_index_lock = asyncio.Lock()
        self._channel_sessions = self._load_channel_sessions()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._pages: dict[str, Any] = {}

    @staticmethod
    def _limit_prompt(prompt: str, limit: int = 12_000) -> str:
        clean = (prompt or "").strip()
        if len(clean) <= limit:
            return clean
        marker = "\n\n[older context trimmed]\n\n"
        available = max(0, limit - len(marker))
        first = available // 2
        last = available - first
        return f"{clean[:first]}{marker}{clean[-last:]}"

    @staticmethod
    def _looks_like_challenge(text: str) -> bool:
        lowered = (text or "").lower()
        return any(marker in lowered for marker in _CHALLENGE_MARKERS)

    @staticmethod
    def _clean_answer(text: str) -> str:
        clean = (text or "").strip()
        clean = re.sub(
            r"^(?:markdown\s*)?(?:Copy\s*)?(?:Download\s*)?",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(r"^```(?:markdown)?\s*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\s*```$", "", clean)
        clean = re.sub(
            r"\s*\[(?:reference|citation|source):?\d+\]",
            "",
            clean,
            flags=re.IGNORECASE,
        )
        clean = re.sub(r"(?m)^\s*(?:[-–—]\s*)?\d{1,3}\s*$", "", clean)
        clean = re.sub(r"(?m)^\s*[-–—]\s*$", "", clean)
        clean = re.sub(r"[-–—](?=[.,;:!?](?:\s|$))", "", clean)
        clean = re.sub(
            r"(?m)^(• \*\*[^\n]+\*\*)\n(?=\S)",
            r"\1\n\n",
            clean,
        )
        # Strip trailing sources block (DeepSeek native UI)
        clean = re.sub(r"(?i)\bSources(?:\s*https?://[^\s]+)+\s*$", "", clean)
        clean = re.sub(r"\n{3,}", "\n\n", clean)
        return clean.strip()

    @classmethod
    def _parse_completion_stream(cls, body: bytes) -> tuple[str, list[str]]:
        final_content = ""
        source_links: list[str] = []
        seen_links: set[str] = set()

        def collect_urls(value: Any) -> None:
            if isinstance(value, dict):
                url = value.get("url")
                if isinstance(url, str):
                    clean_url = cls._sanitize_source_url(url)
                    if clean_url and clean_url not in seen_links:
                        seen_links.add(clean_url)
                        source_links.append(clean_url)
                for child in value.values():
                    collect_urls(child)
            elif isinstance(value, list):
                for child in value:
                    collect_urls(child)

        for line in body.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            content = event.get("content")
            if isinstance(content, str) and content.strip():
                final_content = content
            collect_urls(event)

        return cls._clean_answer(final_content), source_links[:12]

    @staticmethod
    def _sanitize_source_url(url: str) -> str:
        try:
            parsed = urlsplit(url.strip())
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        if parsed.netloc.lower().endswith("deepseek.com"):
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    @staticmethod
    def _clean_session_name(name: str) -> str:
        clean = re.sub(r"[\x00-\x1f\x7f]+", " ", str(name or ""))
        return re.sub(r"\s+", " ", clean).strip()[:100]

    @staticmethod
    def _normalize_session_url(url: str) -> str:
        try:
            parsed = urlsplit(str(url or "").strip())
        except ValueError:
            return ""
        if parsed.scheme != "https" or parsed.netloc.lower() != "chat.deepseek.com":
            return ""
        if not re.fullmatch(r"/a/chat/s/[A-Za-z0-9-]{16,80}", parsed.path):
            return ""
        return urlunsplit(("https", "chat.deepseek.com", parsed.path, "", ""))

    def _load_channel_sessions(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.session_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}

        sessions: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            url = self._normalize_session_url(value.get("url", ""))
            if not url:
                continue
            sessions[key] = {
                "url": url,
                "name": self._clean_session_name(value.get("name", "")),
                "renamed": value.get("renamed") is True,
            }
        return sessions

    def _write_channel_sessions(self) -> None:
        self.session_index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.session_index_path.with_suffix(
            self.session_index_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(self._channel_sessions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.session_index_path)

    async def _save_channel_sessions(self) -> None:
        try:
            await asyncio.to_thread(self._write_channel_sessions)
        except OSError:
            logger.warning("Could not persist DeepSeek channel sessions", exc_info=True)

    @staticmethod
    def _session_key_from_lane(lane: str) -> str:
        return lane[5:] if lane.startswith("chat:") else ""

    def _saved_session_url(self, lane: str) -> str:
        key = self._session_key_from_lane(lane)
        value = self._channel_sessions.get(key, {})
        return self._normalize_session_url(value.get("url", ""))

    async def _forget_channel_session(self, lane: str) -> None:
        key = self._session_key_from_lane(lane)
        if not key:
            return
        async with self._session_index_lock:
            if self._channel_sessions.pop(key, None) is not None:
                await self._save_channel_sessions()

    async def _start(self) -> None:
        async with self._start_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            if not self.storage_state_path.is_file():
                raise DeepSeekWebAuthError(
                    "DeepSeek browser session is not configured."
                )
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise DeepSeekWebError("Playwright is not installed.") from exc

            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=["--disable-dev-shm-usage", "--no-sandbox"],
                )
                self._context = await self._browser.new_context(
                    storage_state=str(self.storage_state_path),
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/148.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1440, "height": 1000},
                )
            except Exception:
                await self.close()
                raise

    def _lock_for_lane(self, lane: str) -> asyncio.Lock:
        lock = self._lane_locks.get(lane)
        if lock is None:
            lock = asyncio.Lock()
            self._lane_locks[lane] = lock
        return lock

    async def _get_page(self, lane: str, *, reuse_existing: bool = False) -> Any:
        for attempt in range(2):
            await self._start()
            failed_browser = self._browser
            page = self._pages.get(lane)
            if page is None and lane.startswith("chat:"):
                async with self._lock_for_lane("chat"):
                    warm_page = self._pages.pop("chat", None)
                    if warm_page is not None and not warm_page.is_closed():
                        page = warm_page
                        self._pages[lane] = page
            is_new_page = page is None or page.is_closed()
            if is_new_page:
                page = await self._context.new_page()
                self._pages[lane] = page
            try:
                if reuse_existing:
                    saved_url = self._saved_session_url(lane)
                    if saved_url and self._normalize_session_url(page.url) != saved_url:
                        await page.goto(
                            saved_url,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                        if self._normalize_session_url(page.url) != saved_url:
                            await self._forget_channel_session(lane)
                    elif is_new_page:
                        await page.goto(
                            _DEEPSEEK_URL,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                    return page
                if not is_new_page and await self._start_spa_chat(page):
                    return page
                await page.goto(
                    _DEEPSEEK_URL,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                return page
            except Exception as exc:
                if attempt > 0 or not self._is_browser_crash_error(exc):
                    raise
                logger.warning(
                    "DeepSeek browser crashed during navigation; restarting once"
                )
                await self._restart_browser(failed_browser)
        raise DeepSeekWebError("DeepSeek browser could not open a chat page.")

    async def _start_spa_chat(self, page: Any) -> bool:
        try:
            candidates = page.get_by_text("New chat", exact=True)
            button = await self._visible_locator(candidates)
            if button is None:
                return False
            await button.click(timeout=5_000)
            await page.wait_for_function(
                "selector => document.querySelectorAll(selector).length === 0",
                arg=_ANSWER_SELECTOR,
                timeout=10_000,
            )
            await self._wait_for_textbox(page)
            return True
        except Exception as exc:
            if self._is_browser_crash_error(exc):
                raise
            logger.debug("DeepSeek SPA new-chat reset failed", exc_info=True)
            return False

    async def prewarm(self) -> None:
        """Open reusable chat and research pages before the first user request."""
        if not self.enabled:
            return
        warmed: list[str] = []
        for lane in ("chat", "research"):
            async with self._lane_locks[lane]:
                try:
                    page = await self._get_page(lane)
                    await self._wait_for_textbox(page)
                    warmed.append(lane)
                except DeepSeekWebError as exc:
                    logger.warning("DeepSeek %s prewarm failed: %s", lane, exc)
                except Exception:
                    logger.exception("DeepSeek %s prewarm failed", lane)
        if warmed:
            logger.info("DeepSeek prewarmed lanes: %s", ", ".join(warmed))

    async def _touch_and_prune_chat_sessions(self, keep_lane: str) -> None:
        now = time.monotonic()
        self._session_last_used[keep_lane] = now
        session_lanes = [
            lane for lane in self._session_last_used if lane.startswith("chat:")
        ]
        expired = {
            lane
            for lane in session_lanes
            if lane != keep_lane
            and now - self._session_last_used[lane] > self.chat_session_ttl
        }
        remaining = sorted(
            (lane for lane in session_lanes if lane not in expired),
            key=self._session_last_used.__getitem__,
        )
        excess = max(0, len(remaining) - self.max_chat_sessions)
        expired.update(lane for lane in remaining[:excess] if lane != keep_lane)
        for lane in expired:
            lock = self._lane_locks.get(lane)
            if lock is not None and lock.locked():
                continue
            await self._discard_page(lane)
            self._lane_locks.pop(lane, None)

    @staticmethod
    def _is_browser_crash_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "page crashed",
                "browser has been closed",
                "browser disconnected",
                "connection closed",
                "target page, context or browser has been closed",
            )
        )

    async def _restart_browser(self, failed_browser: Any) -> None:
        async with self._start_lock:
            if self._browser is not failed_browser:
                return
            await self.close()

    @staticmethod
    async def _visible_locator(locator: Any) -> Any | None:
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
        return None

    async def _wait_for_textbox(self, page: Any) -> Any:
        selectors = (
            page.get_by_role("textbox", name=re.compile(r"Message\s+DeepSeek", re.I)),
            page.locator(
                "textarea[placeholder*='Message DeepSeek' i], "
                "[contenteditable='true'][data-placeholder*='Message DeepSeek' i], "
                "[contenteditable='true'][aria-label*='Message DeepSeek' i], "
                "div[role='textbox'][contenteditable='true']"
            ),
        )
        deadline = time.monotonic() + 30
        last_error: Exception | None = None
        try:
            while time.monotonic() < deadline:
                for locator in selectors:
                    try:
                        textbox = await self._visible_locator(locator)
                        if textbox is not None:
                            await self._ensure_cookie_choice(page)
                            return textbox
                    except Exception as exc:
                        last_error = exc
                await asyncio.sleep(0.25)
            raise TimeoutError("DeepSeek prompt textbox was not visible.") from last_error
        except Exception as exc:
            body_text = ""
            try:
                body_text = await page.locator("body").inner_text(timeout=2_000)
            except Exception:
                pass
            reason = (
                "DeepSeek showed an interactive human verification challenge. "
                "The saved browser session must be renewed manually."
                if self._looks_like_challenge(body_text)
                else "DeepSeek login expired or the saved browser session is invalid."
            )
            raise DeepSeekWebAuthError(reason) from exc

    async def _ensure_cookie_choice(self, page: Any) -> None:
        """Dismiss DeepSeek's consent overlay using necessary cookies only."""
        async with self._cookie_choice_lock:
            candidates = page.get_by_text("Necessary cookies only", exact=True)
            button = await self._visible_locator(candidates)
            if button is None:
                return
            await button.click(timeout=5_000)
            try:
                await candidates.first.wait_for(state="hidden", timeout=5_000)
            except Exception:
                pass
            if self._context is not None:
                await self._context.storage_state(path=str(self.storage_state_path))

    async def _set_mode(self, page: Any, label: str) -> None:
        option = await self._visible_locator(page.get_by_text(label, exact=True))
        if option is None:
            if label == "Instant":
                return
            raise DeepSeekWebError(f"DeepSeek {label} mode was not found.")
        selected = (
            await option.get_attribute("aria-selected") == "true"
            or await option.get_attribute("aria-pressed") == "true"
        )
        classes = (await option.get_attribute("class") or "").lower()
        if not selected and "selected" not in classes and "active" not in classes:
            await option.click()

    async def _set_toggle(self, page: Any, label: str, enabled: bool) -> None:
        candidates = page.locator("div.ds-toggle-button").filter(has_text=label)
        toggle = await self._visible_locator(candidates)
        if toggle is None:
            raise DeepSeekWebError(f"DeepSeek {label} control was not found.")
        current = await toggle.get_attribute("aria-pressed") == "true"
        if current != enabled:
            await toggle.click()
            updated = await toggle.get_attribute("aria-pressed") == "true"
            if updated != enabled:
                state = "enabled" if enabled else "disabled"
                raise DeepSeekWebError(
                    f"DeepSeek {label} could not be {state}."
                )

    async def _attach_images(
        self,
        page: Any,
        images: Sequence[tuple[str, str, bytes]],
    ) -> None:
        file_input = page.locator('input[type="file"]')
        if await file_input.count() == 0:
            attachment_button = page.get_by_role(
                "button",
                name=re.compile(r"attach|upload|image", re.IGNORECASE),
            )
            button = await self._visible_locator(attachment_button)
            if button is not None:
                await button.click()
                file_input = page.locator('input[type="file"]')
        if await file_input.count() == 0:
            raise DeepSeekWebError("DeepSeek Vision upload control was not found.")

        payloads = [
            {"name": name, "mimeType": mime_type, "buffer": data}
            for name, mime_type, data in images
            if data
        ]
        if not payloads:
            raise DeepSeekWebError("No valid image data was supplied.")
        await file_input.first.set_input_files(payloads)
        await self._wait_for_image_ready(page)

    async def _wait_for_image_ready(self, page: Any) -> None:
        """Wait until DeepSeek has uploaded and processed every attachment."""
        ready_button = page.locator(
            "div.ds-button--primary.ds-button--circle"
            ":not(.ds-button--disabled)"
        ).first
        try:
            await ready_button.wait_for(
                state="visible",
                timeout=min(30, self.timeout_seconds) * 1_000,
            )
        except Exception as exc:
            raise DeepSeekWebError(
                "DeepSeek did not finish processing the attached image."
            ) from exc

    async def _configure_request_mode(
        self,
        page: Any,
        *,
        ui_mode: str,
        reuse_existing: bool,
    ) -> None:
        # A restored Vision conversation is already permanently in Vision mode.
        # Its visible "Vision" text is a header label, not the mode picker.
        restored_vision = (
            ui_mode == "Vision"
            and reuse_existing
            and bool(self._normalize_session_url(page.url))
        )
        if not restored_vision:
            await self._set_mode(page, ui_mode)

    async def _prepare_image_submission(
        self,
        page: Any,
        textbox: Any,
        prompt: str,
        images: Sequence[tuple[str, str, bytes]],
    ) -> None:
        # Filling after an upload makes DeepSeek discard the image preview.
        await textbox.fill(prompt)
        await self._attach_images(page, images)

    async def _submit_prompt(
        self,
        page: Any,
        textbox: Any,
        *,
        has_images: bool,
    ) -> None:
        if not has_images:
            send_button = await self._visible_locator(
                page.locator(
                    "div.ds-button--primary.ds-button--circle"
                    ":not(.ds-button--disabled)"
                )
            )
            if send_button is not None:
                await send_button.click(timeout=5_000)
                return
            await textbox.press("Enter")
            return
        send_button = await self._visible_locator(
            page.locator(
                "div.ds-button--primary.ds-button--circle"
                ":not(.ds-button--disabled)"
            )
        )
        if send_button is None:
            raise DeepSeekWebError("DeepSeek image send button was not available.")
        await send_button.click(timeout=5_000)

    async def _extract_answer(self, answer: Any) -> tuple[str, list[str]]:
        text = await answer.evaluate(
            r"""element => {
                const clone = element.cloneNode(true);
                clone.querySelectorAll('a').forEach(anchor => {
                    const text = (anchor.innerText || '').trim();
                    if (/^[\s\d\u002d\u2013\u2014]+$/.test(text)) anchor.remove();
                });

                const render = node => {
                    if (node.nodeType === Node.TEXT_NODE) {
                        return node.textContent || '';
                    }
                    if (node.nodeType !== Node.ELEMENT_NODE) return '';

                    const tag = node.tagName.toLowerCase();
                    const children = [...node.childNodes].map(render).join('');
                    const trimmed = children.trim();
                    if (!trimmed && tag !== 'br') return '';

                    if (/^h[1-6]$/.test(tag)) {
                        const level = Number(tag.slice(1));
                        return `${'#'.repeat(level)} ${trimmed}\n\n`;
                    }
                    if (tag === 'p') return `${trimmed}\n\n`;
                    if (tag === 'br') return '\n';
                    if (tag === 'strong' || tag === 'b') return `**${trimmed}**`;
                    if (tag === 'em' || tag === 'i') return `*${trimmed}*`;
                    if (tag === 'code' && node.parentElement?.tagName.toLowerCase() !== 'pre') {
                        return `\`${trimmed}\``;
                    }
                    if (tag === 'pre') return `\`\`\`\n${node.innerText.trim()}\n\`\`\`\n\n`;
                    if (tag === 'blockquote') {
                        return `${trimmed.split('\n').map(line => `> ${line}`).join('\n')}\n\n`;
                    }
                    if (tag === 'li') {
                        const parent = node.parentElement;
                        let marker = '•';
                        if (parent?.tagName.toLowerCase() === 'ol') {
                            marker = `${[...parent.children].indexOf(node) + 1}.`;
                        }
                        return `${marker} ${trimmed}\n\n`;
                    }
                    if (tag === 'ul' || tag === 'ol') return `${children.trim()}\n\n`;
                    if (tag === 'a') return trimmed;
                    if (tag === 'hr') return '\n---\n\n';
                    return children;
                };

                return render(clone)
                    .replace(/[ \\t]+\n/g, '\n')
                    .replace(/\n{3,}/g, '\n\n')
                    .trim();
            }"""
        )
        links = await answer.evaluate(
            """element => [...new Set(
                [...element.querySelectorAll('a[href]')]
                    .map(anchor => {
                        const url = new URL(anchor.href);
                        url.search = '';
                        url.hash = '';
                        return url.toString();
                    })
                    .filter(url => url.startsWith('http') && !url.includes('deepseek.com'))
            )]"""
        )
        return self._clean_answer(str(text)), [str(link) for link in links]

    async def _wait_for_answer(
        self,
        page: Any,
        before_count: int,
        before_fingerprint: str,
        *,
        stable_reads_required: int,
    ) -> tuple[str, list[str]]:
        answers = page.locator(_ANSWER_SELECTOR)
        deadline = time.monotonic() + self.timeout_seconds
        last_text = ""
        last_links: list[str] = []
        stable_reads = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.75)
            count = await answers.count()
            if count == 0:
                continue
            latest = answers.nth(count - 1)
            fingerprint = (await latest.inner_text()).strip()
            if not self._is_new_answer(
                before_count,
                before_fingerprint,
                count,
                fingerprint,
            ):
                continue
            text, links = await self._extract_answer(latest)
            if not text:
                continue
            if text == last_text:
                stable_reads += 1
            else:
                last_text = text
                last_links = links
                stable_reads = 0
            if stable_reads >= stable_reads_required:
                return text, last_links
        if last_text:
            logger.warning(
                "DeepSeek answer did not stabilize before timeout; returning latest text"
            )
            return last_text, last_links
        raise DeepSeekWebError(
            "DeepSeek timed out before a final answer was returned."
        )

    async def _copy_rendered_answer(
        self,
        page: Any,
        before_count: int,
        before_fingerprint: str,
        *,
        timeout_seconds: float = 3.0,
    ) -> tuple[str, list[str]]:
        """Copy the completed assistant message immediately after the stream ends."""
        answers = page.locator(_ANSWER_SELECTOR)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            count = await answers.count()
            if count:
                latest = answers.nth(count - 1)
                fingerprint = (await latest.inner_text()).strip()
                if self._is_new_answer(
                    before_count,
                    before_fingerprint,
                    count,
                    fingerprint,
                ):
                    text, links = await self._extract_answer(latest)
                    if text:
                        return text, links
            await asyncio.sleep(0.05)
        return "", []

    @staticmethod
    def _is_new_answer(
        before_count: int,
        before_fingerprint: str,
        current_count: int,
        current_fingerprint: str,
    ) -> bool:
        if not current_fingerprint:
            return False
        return (
            current_count > before_count
            or current_fingerprint != before_fingerprint
        )

    async def _run(
        self,
        prompt: str,
        *,
        lane: str,
        ui_mode: str,
        deepthink: bool,
        search: bool,
        images: Sequence[tuple[str, str, bytes]] = (),
        reuse_existing: bool = False,
        session_name: str | None = None,
    ) -> str:
        if not self.enabled:
            raise DeepSeekWebError("DeepSeek web provider is disabled.")
        clean_prompt = self._limit_prompt(prompt)
        if not clean_prompt:
            raise DeepSeekWebError("DeepSeek prompt is empty.")

        async with self._lock_for_lane(lane):
            try:
                page = await self._get_page(
                    lane,
                    reuse_existing=reuse_existing,
                )
                textbox = await self._wait_for_textbox(page)
                await self._configure_request_mode(
                    page,
                    ui_mode=ui_mode,
                    reuse_existing=reuse_existing,
                )
                await self._set_toggle(page, "DeepThink", deepthink)
                if not images:
                    await self._set_toggle(page, "Search", search)
                if images:
                    await self._prepare_image_submission(
                        page,
                        textbox,
                        clean_prompt,
                        images,
                    )

                answers = page.locator(_ANSWER_SELECTOR)
                before_count = await answers.count()
                before_fingerprint = ""
                if before_count:
                    before_fingerprint = (
                        await answers.nth(before_count - 1).inner_text()
                    ).strip()
                answer = ""
                source_links: list[str] = []
                stream_source_links: list[str] = []
                try:
                    async with page.expect_response(
                        lambda response: (
                            "/api/v0/chat/completion" in response.url
                            and response.request.method == "POST"
                        ),
                        timeout=self.timeout_seconds * 1_000,
                    ) as pending_response:
                        if not images:
                            await textbox.fill(clean_prompt)
                        await self._submit_prompt(
                            page,
                            textbox,
                            has_images=bool(images),
                        )
                    response = await pending_response.value
                    body = await response.body()
                    # Top-level stream content can be DeepSeek's generated chat
                    # title rather than the assistant response. The completed
                    # stream is only a fast completion signal and source feed;
                    # the rendered assistant node is the reply authority.
                    _stream_content, stream_source_links = (
                        self._parse_completion_stream(body)
                    )
                    answer, rendered_source_links = (
                        await self._copy_rendered_answer(
                            page,
                            before_count,
                            before_fingerprint,
                        )
                    )
                    source_links = rendered_source_links or stream_source_links
                except Exception:
                    logger.warning(
                        "DeepSeek stream capture failed; using DOM fallback",
                        exc_info=True,
                    )

                if not answer:
                    answer, rendered_source_links = await self._wait_for_answer(
                        page,
                        before_count,
                        before_fingerprint,
                        stable_reads_required=5 if lane == "research" else 3,
                    )
                    source_links = rendered_source_links or stream_source_links
                if search and source_links and not any(
                    link in answer for link in source_links
                ):
                    sources = "\n".join(
                        f"- <{link}>" for link in source_links[:8]
                    )
                    answer = f"{answer}\n\n__BOT_SOURCES__\n{sources}"
                if lane.startswith("chat:"):
                    await self._remember_channel_session(
                        lane,
                        page,
                        session_name=session_name,
                    )
                    await self._touch_and_prune_chat_sessions(lane)
                return answer[:24_000]
            except (DeepSeekWebAuthError, DeepSeekWebError):
                await self._discard_page(lane)
                raise
            except Exception as exc:
                await self._discard_page(lane)
                logger.exception("DeepSeek %s lane failed", lane)
                raise DeepSeekWebError(
                    f"DeepSeek browser failure: {type(exc).__name__}"
                ) from exc

    async def chat(
        self,
        prompt: str,
        *,
        search: bool = False,
        session_key: str | None = None,
        session_name: str | None = None,
        continue_session: bool = False,
        long_answer: bool = False,
        deepthink: bool = False,
    ) -> str:
        if not continue_session:
            if search:
                if long_answer:
                    search_instruction = (
                        "Live web search is enabled. Verify factual, current, game-build, patch, "
                        "character, product, and recommendation claims before answering. Do not "
                        "emit citation tokens, a Sources section, or raw source URLs; the bot "
                        "attaches sources separately. For searchable or factual requests, give "
                        "a thorough 250 to 500 word answer when the topic supports it. Lead with "
                        "the answer, then add useful context, key details, practical guidance, "
                        "and caveats in short paragraphs or bullets. Avoid filler and repetition. "
                    )
                else:
                    search_instruction = (
                        "Live web search is enabled to verify facts, but this is a CASUAL CHAT. "
                        "You MUST give a concise, direct answer (1 to 4 sentences maximum). "
                        "NEVER write essays, multiple paragraphs, or bulleted lists. "
                        "Do not emit citation tokens or URLs. Be brief, punchy, and conversational."
                    )
            else:
                if long_answer:
                    search_instruction = (
                        "Live web search is disabled. Do not claim live verification, add citations, "
                        "or invent external facts. Provide a thorough long-form response that follows "
                        "the requested structure and length. Multiple sections, paragraphs, and bullets "
                        "are allowed when the request calls for them. Avoid filler and repetition. "
                    )
                else:
                    search_instruction = (
                        "Do not claim live verification or add citations. "
                        "You MUST give a concise, direct answer (1 to 4 sentences maximum). "
                        "NEVER write essays, multiple paragraphs, or bulleted lists."
                    )

            request = (
                "Reply in the same language as the user's latest message; default to "
                "English when unclear. Return only the final Discord-ready answer. "
                f"{search_instruction}Do not expose reasoning.\n\n{prompt}"
            )
        else:
            request = prompt

        return await self._run(
            request,
            lane=f"chat:{session_key}" if session_key else "chat",
            ui_mode="Instant",
            deepthink=deepthink,
            search=search,
            reuse_existing=bool(session_key),
            session_name=session_name,
        )

    async def research(self, prompt: str) -> str:
        request = (
            "Research the request using live web search. Reply in the same language "
            "as the user's latest message; default to English when unclear. Return "
            "only the polished Discord-ready final answer. Use this exact visual "
            "structure: begin with one '# <relevant emoji> <specific title>' heading; "
            "then a short direct answer; then organize useful details into clearly "
            "named sections. Format each key section as '• **Section name**' followed "
            "by its explanation on the next indented line. Put a blank line between "
            "every paragraph and section so the response never becomes a wall of text. "
            "Use natural prose, correct punctuation spacing, and bold only the details "
            "that deserve emphasis. Do not add a generic introduction, conclusion, "
            "Sources section, raw URLs, or numeric citation markers; source links are "
            "attached separately by the bot. Do not expose reasoning.\n\n"
            f"{prompt}"
        )
        return await self._run(
            request,
            lane="research",
            ui_mode="Instant",
            deepthink=True,
            search=True,
        )

    async def vision(
        self,
        prompt: str,
        images: Sequence[tuple[str, str, bytes]],
        *,
        search: bool = False,
        session_key: str | None = None,
        session_name: str | None = None,
    ) -> str:
        request = (
            "Analyze the attached image(s) and answer the user's request. Reply in "
            "the same language as the user's latest message; default to English. "
            "Return only the final Discord-ready answer and do not expose reasoning.\n\n"
            f"{prompt}"
        )
        return await self._run(
            request,
            lane=f"chat:{session_key}" if session_key else "vision",
            ui_mode="Vision",
            deepthink=False,
            search=search,
            images=images,
            reuse_existing=bool(session_key),
            session_name=session_name,
        )

    async def _rename_current_chat(self, page: Any, session_name: str) -> bool:
        clean_name = self._clean_session_name(session_name)
        current_url = self._normalize_session_url(page.url)
        if not clean_name or not current_url:
            return False

        path = urlsplit(current_url).path
        link = page.locator(f'a[href="{path}"]')
        link_count = await link.count()
        if link_count != 1 or not await link.is_visible():
            await page.keyboard.press("Control+J")
            await asyncio.sleep(0.2)
            link = page.locator(f'a[href="{path}"]')
        if await link.count() != 1 or not await link.is_visible():
            return False

        await link.hover()
        menu_button = link.locator('[role="button"]')
        if await menu_button.count() != 1:
            return False
        await menu_button.click(timeout=5_000)

        rename_option = await self._visible_locator(
            page.get_by_text("Rename", exact=True)
        )
        if rename_option is None:
            return False
        await rename_option.click(timeout=5_000)

        rename_input = await self._visible_locator(
            page.locator('input.ds-input__input[maxlength="1024"]')
        )
        if rename_input is None:
            return False
        await rename_input.fill(clean_name)
        await rename_input.press("Enter")
        await page.wait_for_function(
            """({path, title}) => [...document.querySelectorAll('a[href]')].some(
                link => link.getAttribute('href') === path
                    && (link.innerText || '').trim() === title
            )""",
            arg={"path": path, "title": clean_name},
            timeout=5_000,
        )
        return True

    async def _remember_channel_session(
        self,
        lane: str,
        page: Any,
        *,
        session_name: str | None,
    ) -> None:
        key = self._session_key_from_lane(lane)
        url = self._normalize_session_url(page.url)
        if not key or not url:
            return

        clean_name = self._clean_session_name(session_name or "")
        previous = self._channel_sessions.get(key, {})
        already_renamed = (
            previous.get("url") == url
            and previous.get("name") == clean_name
            and previous.get("renamed") is True
        )
        renamed = already_renamed
        if clean_name and not already_renamed:
            try:
                renamed = await self._rename_current_chat(page, clean_name)
            except Exception:
                renamed = False
                logger.warning(
                    "Could not rename DeepSeek channel session %s",
                    key,
                    exc_info=True,
                )

        async with self._session_index_lock:
            self._channel_sessions[key] = {
                "url": url,
                "name": clean_name,
                "renamed": renamed,
            }
            await self._save_channel_sessions()

    async def _discard_page(self, lane: str) -> None:
        self._session_last_used.pop(lane, None)
        page = self._pages.pop(lane, None)
        if page is not None and not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass

    async def close(self) -> None:
        for lane in list(self._pages):
            await self._discard_page(lane)
        context, browser, playwright = (
            self._context,
            self._browser,
            self._playwright,
        )
        self._context = self._browser = self._playwright = None
        self._session_last_used.clear()
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
