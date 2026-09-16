"""Bounded incremental stdout/stderr redaction before any disk write."""

from __future__ import annotations

import codecs
from collections import Counter

from orc.dlp_matcher import DlpMatcher
from orc.dlp_models import DlpStatus, StreamRedactionResult, normalize_category_counts


class StreamingRedactor:
    """Keep a fixed overlap, redact complete spans, and cap emitted bytes."""

    def __init__(
        self,
        matcher: DlpMatcher,
        *,
        overlap_chars: int = 64 * 1024,
        output_limit_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if overlap_chars < matcher.minimum_stream_overlap_chars:
            raise ValueError("stream overlap is below matcher safety minimum")
        if output_limit_bytes < 1:
            raise ValueError("stream output limit must be positive")
        self.matcher = matcher
        self.overlap_chars = overlap_chars
        self.output_limit_bytes = output_limit_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._counts: Counter[str] = Counter()
        self._bytes_written = 0
        self._limit_exceeded = False
        self._finished = False
        self._discarding_private_key = False
        self._private_key_tail = ""

    def feed(self, chunk: bytes) -> bytes:
        """Consume one raw chunk and return only safe redacted bytes."""
        if self._finished:
            raise ValueError("stream redactor is already finished")
        decoded = self._decoder.decode(chunk, final=False)
        self._buffer += self._consume_discarded_private_key(decoded, final=False)
        return self._drain(final=False)

    def finish(self) -> bytes:
        """Flush decoder and retained overlap after EOF or process termination."""
        if self._finished:
            return b""
        decoded = self._decoder.decode(b"", final=True)
        self._buffer += self._consume_discarded_private_key(decoded, final=True)
        self._finished = True
        return self._drain(final=True)

    @property
    def buffered_chars(self) -> int:
        """Expose a non-content metric for bounded-memory tests and diagnostics."""
        return len(self._buffer) + len(self._private_key_tail)

    def result(self) -> StreamRedactionResult:
        """Return safe structured state after any amount of input."""
        counts = normalize_category_counts(dict(self._counts)) if self._counts else ()
        if self._limit_exceeded:
            status = DlpStatus.QUARANTINED
            reason = "OUTPUT_LIMIT_EXCEEDED"
        elif counts:
            status = DlpStatus.REDACTED
            reason = "DLP_REDACTED"
        else:
            status = DlpStatus.CLEAN
            reason = "CLEAN"
        return StreamRedactionResult(
            status,
            counts,
            self._bytes_written,
            self._limit_exceeded,
            reason,
            self.matcher.version,
        )

    def _drain(self, *, final: bool) -> bytes:
        if not self._buffer:
            return b""
        private_output = self._bound_incomplete_private_key(final=final)
        if private_output is not None:
            return private_output
        consume = len(self._buffer) if final else len(self._buffer) - self.overlap_chars
        if consume <= 0:
            return b""
        matches = self.matcher.matches(self._buffer)
        changed = True
        while changed:
            changed = False
            for match in matches:
                if match.start() < consume < match.end():
                    consume = match.start()
                    changed = True
        if consume <= 0:
            return b""
        segment = self._buffer[:consume]
        self._buffer = self._buffer[consume:]
        redacted = self.matcher.redact(segment)
        self._counts.update(dict(redacted.category_counts))
        return self._bounded_encode(redacted.text)

    def _bound_incomplete_private_key(self, *, final: bool) -> bytes | None:
        if final or len(self._buffer) <= self.matcher.private_key_stream_buffer_chars:
            return None
        header_start = self.matcher.private_key_header_start(self._buffer)
        if header_start is None:
            return None
        prefix = self._buffer[:header_start]
        private_tail = self._buffer[header_start:]
        footer_end = self.matcher.private_key_footer_end(private_tail)
        if footer_end is None:
            self._discarding_private_key = True
            keep = self.matcher.private_key_marker_overlap_chars
            self._private_key_tail = private_tail[-keep:]
            self._buffer = ""
        else:
            self._buffer = private_tail[footer_end:]
        redacted_prefix = self.matcher.redact(prefix)
        self._counts.update(dict(redacted_prefix.category_counts))
        self._counts["PRIVATE_KEY"] += 1
        output = self._bounded_encode(redacted_prefix.text + "[REDACTED:PRIVATE_KEY]")
        if self._buffer:
            output += self._drain(final=False)
        return output

    def _consume_discarded_private_key(self, text: str, *, final: bool) -> str:
        if not self._discarding_private_key:
            return text
        combined = self._private_key_tail + text
        footer_end = self.matcher.private_key_footer_end(combined)
        if footer_end is not None:
            self._discarding_private_key = False
            self._private_key_tail = ""
            return combined[footer_end:]
        if final:
            self._private_key_tail = ""
            return ""
        keep = self.matcher.private_key_marker_overlap_chars
        self._private_key_tail = combined[-keep:]
        return ""

    def _bounded_encode(self, text: str) -> bytes:
        encoded = text.encode("utf-8")
        remaining = self.output_limit_bytes - self._bytes_written
        if len(encoded) <= remaining:
            self._bytes_written += len(encoded)
            return encoded
        self._limit_exceeded = True
        if remaining <= 0:
            return b""
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if len(text[:middle].encode("utf-8")) <= remaining:
                low = middle
            else:
                high = middle - 1
        bounded = text[:low].encode("utf-8")
        self._bytes_written += len(bounded)
        return bounded
