"""Send console commands and verify acknowledgements in the server log."""
from pathlib import Path
import re
import time


class Console:
    """Match each command's acknowledgement in the local server log."""

    def __init__(self, binary, pane, logfile, work, sender, activity=None):
        self.binary, self.pane, self.logfile = binary, pane, Path(logfile)
        self.work, self.sender = Path(work), sender
        self.activity = activity or (lambda description: None)
        self.work.mkdir(parents=True, exist_ok=True)
        self.sequence = max((int(f.stem) for f in self.work.glob('*.txt') if f.stem.isdigit()), default=0)

    def command(self, command, expected, timeout=60):
        self.activity('Waiting for server acknowledgement: ' + command)
        self.sequence += 1
        command_file = self.work / f'{self.sequence:04d}.txt'
        command_file.write_text(command + '\n')
        offset = self.logfile.stat().st_size
        self.sender(command_file, self.pane, self.binary)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.logfile.open('rb') as stream:
                stream.seek(offset)
                text = stream.read().decode('utf-8', errors='replace')
            marker = 'Handling Console Command ' + command
            if marker in text:
                response = text.split(marker, 1)[1]
                if any(error in response for error in ('Exception:', 'Unknown command', 'No such player')):
                    raise RuntimeError(command + '\n' + response)
                if re.search(expected, response, re.IGNORECASE):
                    (self.work / f'{self.sequence:04d}-response.txt').write_text(text)
                    return response.strip()
            time.sleep(.25)
        raise TimeoutError('No expected acknowledgement for ' + command)
