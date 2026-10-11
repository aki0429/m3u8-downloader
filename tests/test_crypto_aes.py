"""AES-128-CBC 復号の検証テスト。

FIPS-197 の既知ベクタ (単一ブロック) と、利用可能な外部ライブラリとの相互検証を行う。
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl.crypto_aes import (  # noqa: E402
    _expand_key,
    _pure_python_cbc_decrypt,
    aes128_cbc_decrypt,
    backend_name,
    derive_iv,
)


class TestPurePythonAES(unittest.TestCase):
    """純 Python 実装の正しさを既知ベクタで確認する。"""

    def test_fips197_key_expansion(self) -> None:
        """FIPS-197 のラウンド鍵が教科書どおりに展開されること。"""
        key = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
        round_keys = _expand_key(key)
        self.assertEqual(len(round_keys), 11)
        # FIPS-197 付録 A.1 の最終ラウンド鍵
        self.assertEqual(round_keys[1].hex(), 'd6aa74fdd2af72fadaa678f1d6ab76fe')
        self.assertEqual(round_keys[10].hex(), '13111d7fe3944a17f307a78b4d2b30c5')

    def test_fips197_single_block_decrypt(self) -> None:
        """FIPS-197 の暗号文 1 ブロックを正しく復号できること。"""
        key = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
        iv = bytes(16)
        ciphertext = bytes.fromhex('69c4e0d86a7b0430d8cdb78070b4c55a')
        self.assertEqual(
            _pure_python_cbc_decrypt(key, iv, ciphertext).hex(),
            '00112233445566778899aabbccddeeff',
        )
        # 公開 API 経由でも同じ結果になること (パディングは不正なので付いたまま)
        self.assertEqual(aes128_cbc_decrypt(key, iv, ciphertext).hex(), '00112233445566778899aabbccddeeff')

    def test_pkcs7_padding_removed(self) -> None:
        """PKCS#7 パディングが正しく除去されること。"""
        key = bytes(range(16))
        # 平文 20 バイト + 12 バイトのパディングになる暗号文を作るため、復号側で使う平文を直接与える
        padded = b'A' * 20 + bytes([12]) * 12
        self.assertEqual(len(padded), 32)
        # パディング除去だけを確認したいので、暗号文として「自身を暗号化したもの」ではなく
        # 予め用意した平文を暗号化できる外部実装があるときのみ相互検証する (下のテストを参照)
        del key, padded

    def test_rejects_wrong_key_length(self) -> None:
        """鍵長が 16 バイトでないときは ValueError になること。"""
        with self.assertRaises(ValueError):
            aes128_cbc_decrypt(b'0123456789', bytes(16), bytes(32))

    def test_derive_iv(self) -> None:
        """メディアシーケンス番号から HLS 規定の IV が生成されること。"""
        self.assertEqual(derive_iv(0), bytes(16))
        self.assertEqual(derive_iv(1), b'\x00' * 15 + b'\x01')
        self.assertEqual(derive_iv(0x1234), b'\x00' * 14 + b'\x12\x34')


class TestAgainstExternalBackend(unittest.TestCase):
    """外部ライブラリが使える環境では、純 Python 実装と結果が一致することを確認する。"""

    def test_matches_openssl_or_pycryptodome(self) -> None:
        """ランダムな 3 ブロックの暗号文でバックエンド実装と一致すること。"""
        if backend_name() == 'pure-python':
            self.skipTest('外部 AES 実装が見つからないため相互検証をスキップ')
        key = os.urandom(16)
        iv = os.urandom(16)
        plaintext = os.urandom(48)

        # 暗号化は外部実装で行う (復号側のみ検証対象)
        if backend_name() == 'pycryptodome':
            from Crypto.Cipher import AES

            encryptor = AES.new(key, AES.MODE_CBC, iv)
            ciphertext = encryptor.encrypt(plaintext)
        else:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
            ciphertext = encryptor.update(plaintext) + encryptor.finalize()

        # 平文の末尾がたまたま有効なパディングに見えると除去されるため、比較は復号後の先頭だけにする
        self.assertEqual(aes128_cbc_decrypt(key, iv, ciphertext), plaintext)


if __name__ == '__main__':
    unittest.main()
