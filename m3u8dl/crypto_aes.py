"""AES-128-CBC の復号ユーティリティ (HLS の #EXT-X-KEY METHOD=AES-128 用)。

外部ライブラリ (pycryptodome / cryptography) が使える環境ではそちらを利用し、
無い場合は標準ライブラリだけで動く純 Python 実装にフォールバックする。
純 Python 実装は速度が数十 KB/s 程度なので、実行時はログでバックエンド名を通知する。
"""

from __future__ import annotations


def _xtime(value: int) -> int:
    """GF(2^8) 上で 2 倍 (x 倍) する。

    Args:
        value: 対象のバイト値。

    Returns:
        2 倍した結果 (0x1B による剰余済み)。
    """
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _gmul(left: int, right: int) -> int:
    """GF(2^8) 上の積を計算する。

    Args:
        left: 掛けられる値。
        right: 掛ける値。

    Returns:
        積 (0-255)。
    """
    result = 0
    while right:
        if right & 1:
            result ^= left
        left = _xtime(left)
        right >>= 1
    return result


def _build_sbox() -> tuple[list[int], list[int]]:
    """AES の S-Box と逆 S-Box を生成する。

    テーブルをハードコードせず、GF(2^8) の逆元とアフィン変換の定義から計算する
    (定数の書き写しミスを避けるため)。

    Returns:
        (S-Box, 逆 S-Box) のタプル。
    """
    # 生成元 3 による指数・対数表を先に作る (逆元計算用)
    exp_table = [0] * 256
    log_table = [0] * 256
    value = 1
    for index in range(255):
        exp_table[index] = value
        log_table[value] = index
        value ^= _xtime(value)  # value *= 3
    exp_table[255] = exp_table[0]

    sbox = [0] * 256
    for byte in range(256):
        # 0 の逆元は 0 と定義する
        inverse = 0 if byte == 0 else exp_table[255 - log_table[byte]]
        rotated = inverse
        for _ in range(4):
            rotated = ((rotated << 1) | (rotated >> 7)) & 0xFF
            inverse ^= rotated
        sbox[byte] = inverse ^ 0x63

    inverse_sbox = [0] * 256
    for index, byte in enumerate(sbox):
        inverse_sbox[byte] = index
    return sbox, inverse_sbox


_SBOX, _INV_SBOX = _build_sbox()

# 逆 ShiftRows 用の添字テーブル。state[i] の i は r + 4c (r = i % 4, c = i // 4) で、
# 復号側は行 r を右へ r だけローテートするため out[r][c] = in[r][(c - r) % 4] となる。
_INV_SHIFT_ROWS = [i % 4 + 4 * ((i // 4 - i % 4) % 4) for i in range(16)]


def _expand_key(key: bytes) -> list[bytes]:
    """AES-128 の鍵スケジュールを計算する。

    Args:
        key: 16 バイトの秘密鍵。

    Returns:
        11 個のラウンド鍵 (各 16 バイト)。round_keys[0] が初期 AddRoundKey 用。

    Raises:
        ValueError: 鍵長が 16 バイトでないとき。
    """
    if len(key) != 16:
        raise ValueError('AES-128 の鍵長は 16 バイトである必要があります')

    # 最初の 4 ワードは鍵そのもの
    words = [list(key[4 * index:4 * index + 4]) for index in range(4)]
    round_constant = 1
    for index in range(4, 44):
        temp = list(words[index - 1])
        if index % 4 == 0:
            # RotWord + SubWord + Rcon
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[byte] for byte in temp]
            temp[0] ^= round_constant
            round_constant = _xtime(round_constant)
        words.append([words[index - 4][position] ^ temp[position] for position in range(4)])

    # 4 ワードずつを連結してラウンド鍵 (16 バイト) にする
    round_keys: list[bytes] = []
    for round_index in range(11):
        flat = [byte for word in words[4 * round_index:4 * round_index + 4] for byte in word]
        round_keys.append(bytes(flat))
    return round_keys


def _inv_mix_columns(state: list[int]) -> list[int]:
    """InvMixColumns を適用する。

    Args:
        state: 16 バイトの状態 (列優先)。

    Returns:
        変換後の状態。
    """
    result = [0] * 16
    for column in range(4):
        base = column * 4
        a0, a1, a2, a3 = state[base], state[base + 1], state[base + 2], state[base + 3]
        result[base] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        result[base + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        result[base + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        result[base + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
    return result


def _decrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    """AES の 1 ブロック (16 バイト) を復号する。

    Args:
        block: 16 バイトの暗号ブロック。
        round_keys: _expand_key() の結果。

    Returns:
        復号された 16 バイト。
    """
    # 最終ラウンド鍵を先に XOR する (逆順に進む等価逆暗号)
    state = [block[index] ^ round_keys[10][index] for index in range(16)]
    for round_index in range(9, 0, -1):
        # InvShiftRows と InvSubBytes は添字変換で同時に適用できる
        state = [_INV_SBOX[state[_INV_SHIFT_ROWS[index]]] for index in range(16)]
        state = [state[index] ^ round_keys[round_index][index] for index in range(16)]
        state = _inv_mix_columns(state)
    state = [_INV_SBOX[state[_INV_SHIFT_ROWS[index]]] for index in range(16)]
    state = [state[index] ^ round_keys[0][index] for index in range(16)]
    return bytes(state)


def _pure_python_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """純 Python 実装による AES-128-CBC 復号。

    Args:
        key: 16 バイトの鍵。
        iv: 16 バイトの IV。
        data: 16 バイト境界に揃った暗号文。パディングは含んだままでよい。

    Returns:
        復号結果 (パディング除去前)。
    """
    round_keys = _expand_key(key)
    previous = iv
    output = bytearray()
    for offset in range(0, len(data) - 15, 16):
        block = data[offset:offset + 16]
        decrypted = _decrypt_block(block, round_keys)
        # CBC は前ブロックとの XOR で平文を得る
        output.extend(a ^ b for a, b in zip(decrypted, previous))
        previous = block
    return bytes(output)


# 利用可能な最速バックエンドを選択する (定義順に優先度が高い)
_BACKEND = 'pure-python'
_AES = None
try:  # pragma: no cover - 環境依存
    from Crypto.Cipher import AES as _PyCryptodomeAES  # type: ignore[import-not-found]

    _AES = _PyCryptodomeAES
    _BACKEND = 'pycryptodome'
except ImportError:
    pass

if _AES is None:  # pragma: no cover - 環境依存
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        _BACKEND = 'cryptography'
    except ImportError:
        Cipher = None  # type: ignore[assignment]
        algorithms = None  # type: ignore[assignment]
        modes = None  # type: ignore[assignment]


def backend_name() -> str:
    """現在使用中の AES バックエンド名を返す。

    Returns:
        'pycryptodome' / 'cryptography' / 'pure-python' のいずれか。
    """
    return _BACKEND


def derive_iv(media_sequence: int) -> bytes:
    """セグメントのメディアシーケンス番号から HLS 規定の IV を生成する。

    Args:
        media_sequence: セグメントのシーケンス番号。

    Returns:
        16 バイトのビッグエンディアン表現の IV。
    """
    return media_sequence.to_bytes(16, 'big')


def _unpad(data: bytes) -> bytes:
    """PKCS#7 パディングを除去する。

    Args:
        data: 復号後のデータ。

    Returns:
        パディング除去後のデータ。パディングが不正なときはそのまま返す。
    """
    if not data:
        return data
    pad = data[-1]
    # パディング長は 1-16 で、末尾の pad バイトがすべて同じ値であること
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC で復号し、PKCS#7 パディングを除去する。

    Args:
        key: 16 バイトの鍵。
        iv: 16 バイトの IV。
        data: 暗号文。長さが 16 の倍数でない場合は末尾の半端を切り捨てる。

    Returns:
        復号済みの平文。

    Raises:
        ValueError: 鍵または IV の長さが 16 バイトでないとき。
    """
    if len(key) != 16:
        raise ValueError('AES-128 の鍵長は 16 バイトである必要があります')
    if len(iv) != 16:
        raise ValueError('AES-128 の IV 長は 16 バイトである必要があります')

    # 16 バイト境界に揃える (壊れたデータでも例外にせず読める範囲で復号する)
    usable = len(data) - (len(data) % 16)
    if usable <= 0:
        return b''
    payload = data[:usable]

    if _BACKEND == 'pycryptodome':
        decryptor = _AES.new(key, _AES.MODE_CBC, iv)  # type: ignore[union-attr]
        decrypted = decryptor.decrypt(payload)
    elif _BACKEND == 'cryptography':
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(payload) + decryptor.finalize()
    else:
        decrypted = _pure_python_cbc_decrypt(key, iv, payload)

    return _unpad(decrypted)
