"""Progress text formatting shared by database jobs."""


def _format_eta(seconds: int) -> str:
    if seconds < 0:
        return "00:00"
    minutes = seconds // 60
    secs = seconds % 60
    return f"{int(minutes):02d}:{int(secs):02d}"


def _format_elapsed(seconds: float) -> str:
    return _format_eta(int(max(0, seconds)))
