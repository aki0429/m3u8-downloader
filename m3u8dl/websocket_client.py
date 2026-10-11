"""CDP (Chrome DevTools Protocol) 通信用の最小 WebSocket クライアント。

標準ライブラリのみで RFC6455 のクライアントハンドシェイクとフレームの送受信を実装する。
CDP はテキストフレームの JSON しか使わないため、このクラスもテキストメッセージだけを扱う。

受信は socket から読んだ生バイトを内部バッファに溜め、そこからフレームを切り出す方式にしている。
そのため、フレームの途中で受信タイムアウトになっても受信済みのバイト列を失わず、
次回の `recv_text()` で続きから読み直せる。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import time

from m3u8dl.errors import BrowserError

# RFC6455 で定義された WebSocket ハンドシェイク用のマジック文字列 (Sec-WebSocket-Accept の計算に使う)
_WEBSOCKET_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

# RFC6455 のフレームオペコード
_OPCODE_CONTINUATION = 0x0
_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA


class WebSocketConnection:
    """RFC6455 の WebSocket クライアント接続を表す。

    生成時に TCP 接続とハンドシェイクを完了させる (失敗時は `BrowserError`)。
    送信は内部ロックで直列化するので複数スレッドから呼んでよい。

    保持する情報:
        host / port / path: 接続先ホスト・ポート・リクエストパス ('/' を含む)。
        origin: ハンドシェイクで送る Origin ヘッダ。空文字なら送信しない。
        timeout: TCP 接続とハンドシェイクに使う既定タイムアウト (秒)。
    参照されるメソッド:
        `send_text()` は CDP コマンド送信から、`recv_text()` は受信ループから呼ばれる。
    """

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        origin: str = 'http://localhost',
        timeout: float = 10.0,
    ) -> None:
        """WebSocket 接続を確立する。

        Args:
            host: 接続先ホスト名または IP アドレス。
            port: 接続先 TCP ポート。
            path: リクエストパス (先頭に '/' が無ければ補う)。
            origin: ハンドシェイクで送る Origin ヘッダ。空文字なら送信しない。
            timeout: TCP 接続とハンドシェイクのタイムアウト (秒)。

        Raises:
            BrowserError: TCP 接続またはハンドシェイクに失敗したとき。
        """
        self.host = host
        self.port = port
        self.path = path if path.startswith('/') else '/' + path
        self.origin = origin
        self.timeout = timeout

        # 受信途中の生バイトを保持するバッファ。タイムアウトしても破棄しない。
        self._recv_buffer = bytearray()
        # 送信フレームの書き込みを直列化するためのロック (複数スレッドから送信される)
        self._send_lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._closed = False

        self._connect()

    @property
    def closed(self) -> bool:
        """接続が閉じられている (または閉じられた) かどうかを返す。

        Returns:
            閉じられていれば True。
        """
        return self._closed

    def send_text(self, text: str) -> None:
        """テキストフレームを送信する。

        Args:
            text: 送信する文字列 (UTF-8 でエンコードする)。

        Raises:
            BrowserError: 接続が既に閉じられているとき。
        """
        self._send_frame(_OPCODE_TEXT, text.encode('utf-8'))

    def recv_text(self, timeout: float = 1.0) -> str | None:
        """テキストフレームを受信する。

        フラグメント化されたメッセージは FIN が立つまで内部で連結してから返す。
        PING には自動で PONG を返し、CLOSE を受信したら接続を閉じて None を返す。

        Args:
            timeout: 受信を待つ最大時間 (秒)。

        Returns:
            受信したテキスト。タイムアウト・切断・CLOSE 受信時は None。
        """
        deadline = time.monotonic() + timeout
        fragments: list[bytes] = []
        message_opcode: int | None = None
        while True:
            frame = self._next_frame(deadline)
            if frame is None:
                return None
            fin, opcode, payload = frame
            # 制御フレームはメッセージの断片とは独立に処理する
            if opcode == _OPCODE_CLOSE:
                self._closed = True
                return None
            if opcode == _OPCODE_PING:
                try:
                    self._send_frame(_OPCODE_PONG, payload)
                except BrowserError:
                    return None
                continue
            if opcode == _OPCODE_PONG:
                continue
            if opcode in (_OPCODE_TEXT, _OPCODE_BINARY):
                message_opcode = opcode
                fragments = [payload]
            elif opcode == _OPCODE_CONTINUATION:
                if message_opcode is None:
                    # 先頭フレームを取りこぼした continuation は無視する
                    continue
                fragments.append(payload)
            # FIN が立っていれば 1 メッセージとして確定する
            if fin:
                data = b''.join(fragments)
                fragments = []
                return data.decode('utf-8', errors='replace')
            # FIN 未達なら次のフレーム (continuation) を読み続ける

    def close(self) -> None:
        """接続を閉じる。冪等 (何度呼んでも安全)。"""
        sock = self._sock
        if self._closed or sock is None:
            self._closed = True
            return
        # クローズフレームを送ってから切断する (best-effort)。失敗しても切断処理は続ける。
        try:
            self._send_frame(_OPCODE_CLOSE, b'')
        except BrowserError:
            pass
        self._closed = True
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def __enter__(self) -> 'WebSocketConnection':
        """with 文の開始時に自分自身を返す。"""
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """with 文の終了時に接続を閉じる。"""
        self.close()

    def _connect(self) -> None:
        """TCP 接続と WebSocket ハンドシェイクを行う。

        Raises:
            BrowserError: 接続またはハンドシェイクに失敗したとき。
        """
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise BrowserError(f'WebSocket 接続に失敗しました: {self.host}:{self.port} ({exc})') from exc
        self._sock.settimeout(self.timeout)

        # ランダムな 16 バイトを base64 したものを Sec-WebSocket-Key に使う
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        lines = [
            f'GET {self.path} HTTP/1.1',
            f'Host: {self.host}:{self.port}',
            'Upgrade: websocket',
            'Connection: Upgrade',
            f'Sec-WebSocket-Key: {key}',
            'Sec-WebSocket-Version: 13',
        ]
        # Origin は指定があるときだけ送る (Chrome は未指定の接続を常に許可する)
        if self.origin:
            lines.append(f'Origin: {self.origin}')
        request = '\r\n'.join(lines) + '\r\n\r\n'
        try:
            self._sock.sendall(request.encode('ascii'))
        except OSError as exc:
            raise BrowserError(f'WebSocket ハンドシェイク要求の送信に失敗しました: {exc}') from exc

        head = self._read_handshake()
        status, headers = self._parse_handshake(head)
        if status != 101:
            raise BrowserError(f'WebSocket ハンドシェイクに失敗しました (HTTP {status})')
        # サーバーの Sec-WebSocket-Accept が自分が送った鍵から期待される値と一致することを確認する
        expected = base64.b64encode(
            hashlib.sha1((key + _WEBSOCKET_GUID).encode('ascii')).digest()
        ).decode('ascii')
        if headers.get('sec-websocket-accept') != expected:
            raise BrowserError('WebSocket ハンドシェイクの Sec-WebSocket-Accept が一致しません')

    def _read_handshake(self) -> bytes:
        """ハンドシェイク応答のヘッダ部を読み終えるまで受信する。

        Returns:
            ヘッダ部 (ステータス行とヘッダ行、末尾の空行を含まない)。

        Raises:
            BrowserError: 受信中に切断・タイムアウトしたとき。
        """
        assert self._sock is not None
        data = bytearray()
        while b'\r\n\r\n' not in data:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                raise BrowserError('WebSocket ハンドシェイクがタイムアウトしました')
            except OSError as exc:
                raise BrowserError(f'WebSocket ハンドシェイクの受信に失敗しました: {exc}') from exc
            if not chunk:
                raise BrowserError('WebSocket ハンドシェイク中に接続が切断されました')
            data.extend(chunk)
            if len(data) > 65536:
                raise BrowserError('WebSocket ハンドシェイク応答が大きすぎます')
        head, _, rest = bytes(data).partition(b'\r\n\r\n')
        # ヘッダ直後にフレームが来ている場合はバッファへ回す
        if rest:
            self._recv_buffer.extend(rest)
        return head

    @staticmethod
    def _parse_handshake(head: bytes) -> tuple[int, dict[str, str]]:
        """ハンドシェイク応答をステータスコードとヘッダ辞書に分解する。

        Args:
            head: ヘッダ部のバイト列。

        Returns:
            (ステータスコード, 小文字化したヘッダ名→値) のタプル。

        Raises:
            BrowserError: ステータス行が不正なとき。
        """
        text = head.decode('latin-1')
        lines = text.split('\r\n')
        status_line = lines[0] if lines else ''
        parts = status_line.split(' ', 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise BrowserError(f'WebSocket ハンドシェイク応答が不正です: {status_line!r}')
        status = int(parts[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ':' in line:
                name, _, value = line.partition(':')
                headers[name.strip().lower()] = value.strip()
        return status, headers

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        """1 フレームをマスク付きで送信する (クライアント→サーバーは常にマスク必須)。

        Args:
            opcode: フレームのオペコード。
            payload: 送信するペイロード。

        Raises:
            BrowserError: 接続が未確立または閉じられているとき、送信に失敗したとき。
        """
        sock = self._sock
        if sock is None or self._closed:
            raise BrowserError('WebSocket は既に閉じられています')

        length = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)  # FIN=1 + オペコード
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack('>H', length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack('>Q', length))
        mask_key = os.urandom(4)
        header.extend(mask_key)
        masked = bytearray(payload)
        for index in range(length):
            masked[index] ^= mask_key[index % 4]
        with self._send_lock:
            sock.sendall(bytes(header) + bytes(masked))

    def _next_frame(self, deadline: float) -> tuple[bool, int, bytes] | None:
        """バッファ内で 1 フレームが揃うまで受信する。

        Args:
            deadline: `time.monotonic()` 基準の受信締め切り時刻。

        Returns:
            (FIN フラグ, オペコード, ペイロード) のタプル。タイムアウト・切断時は None。
        """
        while True:
            frame = self._extract_frame()
            if frame is not None:
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock = self._sock
            if sock is None:
                return None
            try:
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                self._closed = True
                return None
            if not chunk:
                self._closed = True
                return None
            self._recv_buffer.extend(chunk)

    def _extract_frame(self) -> tuple[bool, int, bytes] | None:
        """受信バッファから 1 フレームを取り出す。

        バッファに完全なフレームが無ければ None を返し、何も消費しない。

        Returns:
            (FIN フラグ, オペコード, ペイロード) のタプル。未完成なら None。
        """
        buffer = self._recv_buffer
        if len(buffer) < 2:
            return None
        first = buffer[0]
        second = buffer[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(buffer) < offset + 2:
                return None
            length = struct.unpack('>H', buffer[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            if len(buffer) < offset + 8:
                return None
            length = struct.unpack('>Q', buffer[offset:offset + 8])[0]
            offset += 8
        mask_key = b''
        if masked:
            if len(buffer) < offset + 4:
                return None
            mask_key = bytes(buffer[offset:offset + 4])
            offset += 4
        if len(buffer) < offset + length:
            return None
        payload = bytes(buffer[offset:offset + length])
        del buffer[:offset + length]
        # サーバー→クライアントのフレームは通常マスクされないが、来た場合は解除する
        if masked:
            payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload
