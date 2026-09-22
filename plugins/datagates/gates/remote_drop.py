"""
Remote drop gate — files fetched from SFTP, FTPS or plain FTP first, then
read like any other drop.

The partner will not push to you; they have an FTP server that has been
there since 2011 and a nightly job that writes into it. This gate pulls
what is new into a local directory and hands it to the CSV, Excel or XML
reader, so the two halves of the problem stay separate.

    - key: district_heating
      type: remote_drop
      options:
        protocol: sftp                      # sftp | ftps | ftp
        host: files.partner.example
        port: 22
        username: ${DH_USER}
        password: ${DH_PASSWORD}            # or key_file for sftp
        key_file: /opt/airflow/secrets/dh_ed25519
        remote_dir: /exports/heat
        directory: /opt/airflow/drop/district_heating    # local mirror
        pattern: "*.csv"
        format: csv                         # csv | excel | xml
        delete_after_download: false
        # ... plus every option of the csv_drop / excel_drop / xml_drop gate
        timestamp_column: Timestamp
        device_column: MeterId
        columns:
          Energy: {property: energy, unit: KWH, cumulative: true}
        devices:
          - {id: "5432", name: Heat meter 5432}

Decisions that keep this working unattended:

- **Download, then parse.** A parse that fails leaves the file on disk to
  look at, and a transfer that fails leaves nothing half-parsed. The
  local directory is the record of what was received.
- **A file is downloaded once**, by name and size; a partner who
  overwrites a file with a corrected version (same name, new size) gets
  it re-downloaded and re-read, which is what a correction should do.
- **`delete_after_download` is off by default.** Deleting from a
  partner's server is irreversible and often forbidden by their own
  retention rules; turn it on only when they ask, usually because their
  disk fills up otherwise.
- **Plain `ftp` sends the password in clear text.** It is supported
  because some systems offer nothing else, and it is worth putting in
  writing in the handover that the credentials on that link are exposed.

SFTP needs the `paramiko` extra; FTP and FTPS use the standard library.
"""
from __future__ import annotations

import ftplib
import logging
import os
from collections.abc import Iterator
from typing import Any

from datagates.gates.csv_drop import CsvDropGate
from datagates.gates.excel_drop import ExcelDropGate
from datagates.gates.file_drop import FileDropGate
from datagates.gates.xml_drop import XmlDropGate

log = logging.getLogger(__name__)

READERS: dict[str, type[FileDropGate]] = {"csv": CsvDropGate, "excel": ExcelDropGate, "xml": XmlDropGate}


class RemoteDropGate(FileDropGate):
    type_name = "remote_drop"
    speaks = "SFTP / FTPS / FTP servers holding exported files"

    def __init__(self, config):
        super().__init__(config)
        self.protocol = str(self.option("protocol", "sftp")).lower()
        if self.protocol not in ("sftp", "ftps", "ftp"):
            raise ValueError(f"{self.key}: protocol must be sftp, ftps or ftp")
        self.host = str(self.required("host"))
        self.port = int(self.option("port", 22 if self.protocol == "sftp" else 21))
        self.username = str(self.option("username", ""))
        self.password = str(self.option("password", ""))
        self.key_file = str(self.option("key_file", ""))
        self.remote_dir = str(self.option("remote_dir", "."))
        self.delete_after_download = bool(self.option("delete_after_download", False))
        self.format = str(self.option("format", "csv")).lower()
        if self.format not in READERS:
            raise ValueError(f"{self.key}: format must be csv, excel or xml")
        # The parsing half is an ordinary drop gate over the local mirror.
        self.reader: FileDropGate = READERS[self.format](config)
        self.pattern = self.reader.pattern
        self.columns = self.reader.columns
        self.cumulative = self.reader.cumulative            # type: ignore[misc]
        self.ts_col, self.device_col = self.reader.ts_col, self.reader.device_col

    # -- transports --------------------------------------------------------
    def _sftp_sync(self) -> int:
        import paramiko  # imported here: optional dependency

        transport = paramiko.Transport((self.host, self.port))
        try:
            if self.key_file:
                transport.connect(username=self.username, pkey=paramiko.PKey.from_path(self.key_file))
            else:
                transport.connect(username=self.username, password=self.password)
            client = paramiko.SFTPClient.from_transport(transport)
            if client is None:                              # pragma: no cover - refused channel
                raise ConnectionError(f"{self.key}: {self.host} refused an SFTP channel")
            downloaded = 0
            for attributes in client.listdir_attr(self.remote_dir):
                name = attributes.filename
                if not self._wanted(name, attributes.st_size or 0):
                    continue
                client.get(f"{self.remote_dir.rstrip('/')}/{name}", os.path.join(self.directory, name))
                downloaded += 1
                if self.delete_after_download:
                    client.remove(f"{self.remote_dir.rstrip('/')}/{name}")
            return downloaded
        finally:
            transport.close()

    def _ftp_sync(self) -> int:
        connection = ftplib.FTP_TLS() if self.protocol == "ftps" else ftplib.FTP()
        connection.connect(self.host, self.port, timeout=int(self.option("timeout", 60)))
        try:
            connection.login(self.username or "anonymous", self.password)
            if isinstance(connection, ftplib.FTP_TLS):
                connection.prot_p()
            connection.cwd(self.remote_dir)
            downloaded = 0
            for name in connection.nlst():
                try:
                    size = connection.size(name) or 0
                except ftplib.error_perm:                   # a directory, or SIZE refused in ASCII mode
                    continue
                if not self._wanted(name, size):
                    continue
                with open(os.path.join(self.directory, name), "wb") as fh:
                    connection.retrbinary(f"RETR {name}", fh.write)
                downloaded += 1
                if self.delete_after_download:
                    connection.delete(name)
            return downloaded
        finally:
            try:
                connection.quit()
            except (ftplib.Error, OSError):                 # pragma: no cover - server already gone
                connection.close()

    def _wanted(self, name: str, size: int) -> bool:
        """Download unless the local mirror already holds the same file at
        the same size."""
        import fnmatch

        if not fnmatch.fnmatch(name, self.pattern):
            return False
        local = os.path.join(self.directory, name)
        return not (os.path.exists(local) and os.path.getsize(local) == size)

    # -- the contract ------------------------------------------------------
    def prepare(self) -> None:
        os.makedirs(self.directory, exist_ok=True)
        downloaded = self._sftp_sync() if self.protocol == "sftp" else self._ftp_sync()
        if downloaded:
            log.info("%s: downloaded %d file(s) from %s:%s", self.key, downloaded, self.host, self.remote_dir)

    def rows(self, path: str) -> Iterator[dict[str, Any]]:
        return self.reader.rows(path)
