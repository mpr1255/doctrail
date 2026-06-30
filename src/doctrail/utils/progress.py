"""Progress bar utilities for Doctrail."""

import itertools
from typing import Optional, Dict, Any
from tqdm import tqdm

from ..constants import SPINNER_CHARS, PROGRESS_BAR_FORMAT


class SpinnerTqdm(tqdm):
    """Custom tqdm with animated spinner for non-verbose mode."""
    
    def __init__(self, *args, **kwargs):
        self.use_spinner = kwargs.pop('use_spinner', False)
        if self.use_spinner:
            self.spinner = itertools.cycle(SPINNER_CHARS)
            kwargs['bar_format'] = '{desc} {spinner} {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} docs [{rate_fmt}]'
        super().__init__(*args, **kwargs)
    
    def format_meter(self, n, total, elapsed, ncols=None, prefix='', ascii=False, 
                     unit='it', unit_scale=False, rate=None, bar_format=None, 
                     postfix=None, unit_divisor=1000, **extra_kwargs):
        if self.use_spinner and hasattr(self, 'spinner'):
            # Replace {spinner} placeholder with actual spinner character
            if bar_format and '{spinner}' in bar_format:
                bar_format = bar_format.replace('{spinner}', next(self.spinner))
        return super().format_meter(n, total, elapsed, ncols, prefix, ascii, 
                                   unit, unit_scale, rate, bar_format, postfix, 
                                   unit_divisor, **extra_kwargs)


def create_progress_bar(total: int, desc: str, verbose: bool = False, 
                       position: Optional[int] = None, 
                       leave: bool = True) -> tqdm:
    """Create a standardized progress bar for Doctrail operations.
    
    Args:
        total: Total number of items to process
        desc: Description for the progress bar
        verbose: Whether to show detailed progress (spinner vs full bar)
        position: Position for multi-bar displays
        leave: Whether to leave the progress bar on screen after completion
        
    Returns:
        Configured tqdm progress bar instance
    """
    if verbose:
        # Full progress bar with detailed info
        return tqdm(
            total=total,
            desc=desc,
            position=position,
            leave=leave,
            bar_format=PROGRESS_BAR_FORMAT
        )
    else:
        # Spinner mode for concise output
        return SpinnerTqdm(
            total=total,
            desc=desc,
            position=position,
            leave=leave,
            use_spinner=True
        )


def update_progress(pbar: tqdm, increment: int = 1, 
                   postfix: Optional[Dict[str, Any]] = None) -> None:
    """Update progress bar with optional postfix information.
    
    Args:
        pbar: Progress bar instance
        increment: Number of items to increment
        postfix: Optional dictionary of values to display after the bar
    """
    if postfix:
        pbar.set_postfix(postfix)
    pbar.update(increment)


def close_progress_bars(*pbars: tqdm) -> None:
    """Safely close multiple progress bars.
    
    Args:
        *pbars: Variable number of progress bar instances to close
    """
    for pbar in pbars:
        if pbar is not None:
            pbar.close()