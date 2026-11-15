"""
Parallel Processing - Process STT and context retrieval simultaneously.

No accuracy loss - just processes things in parallel instead of sequentially.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Optional, Tuple

log = logging.getLogger(__name__)


class ParallelProcessor:
    """
    Parallel processor for overlapping STT and context operations.
    
    Processes STT and context retrieval simultaneously to reduce latency
    without any accuracy trade-offs.
    """
    
    def __init__(self, max_workers: int = 2):
        """
        Initialize parallel processor.
        
        Args:
            max_workers: Maximum number of parallel workers
        """
        self.max_workers = max_workers
    
    def process_stt_and_context(
        self,
        stt_func,
        stt_args: tuple,
        context_func,
        context_args: tuple,
    ) -> Tuple[str, Optional[str]]:
        """
        Process STT and context retrieval in parallel.
        
        Args:
            stt_func: STT transcription function
            stt_args: Arguments for STT function
            context_func: Context retrieval function
            context_args: Arguments for context function
        
        Returns:
            Tuple of (transcribed_text, context_text)
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit both tasks
            stt_future = executor.submit(stt_func, *stt_args)
            context_future = executor.submit(context_func, *context_args)
            
            # Wait for both to complete
            try:
                transcribed_text = stt_future.result(timeout=10.0)
                context_text = context_future.result(timeout=10.0)
                return transcribed_text, context_text
            except concurrent.futures.TimeoutError:
                log.warning("Parallel processing timeout")
                # Fallback to sequential
                transcribed_text = stt_func(*stt_args)
                context_text = context_func(*context_args)
                return transcribed_text, context_text
            except Exception as exc:
                log.warning(f"Parallel processing error: {exc}")
                # Fallback to sequential
                transcribed_text = stt_func(*stt_args)
                context_text = context_func(*context_args)
                return transcribed_text, context_text
