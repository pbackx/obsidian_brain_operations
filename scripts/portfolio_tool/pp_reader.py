#!/usr/bin/env python3
"""
Portfolio Performance File Decryptor, Parser & Sanity Checker.
Supports:
- Password-protected AES-256 / AES-128 .portfolio files (Protobuf & XML)
- Unencrypted Binary / Compressed .portfolio ZIP files
- Plain XML .xml files
"""

import sys
import os
import io
import struct
import zipfile
import argparse
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.padding import PKCS7

# Add current directory for client_pb2
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import client_pb2
except ImportError:
    client_pb2 = None

# Constants from Portfolio Performance ClientFactory.java
SIGNATURE = b"PORTFOLIO"
JAVA_SALT = bytes([112, 67, 103, 107, 164, 131, 144, 161, 159, 142, 117, 200, 203, 187, 231, 228])
PBKDF2_ITERATIONS = 65536
IV_LENGTH = 16


def decrypt_portfolio_data(raw_bytes: bytes, password: str) -> Tuple[int, int, bytes]:
    """
    Decrypts an encrypted Portfolio Performance stream.
    Returns (contentType, version, decrypted_unzipped_bytes).
    contentType: 1 = XML, 2 = PROTOBUF.
    """
    if not raw_bytes.startswith(SIGNATURE):
        raise ValueError(f"File does not start with PORTFOLIO signature (starts with: {raw_bytes[:16]!r})")

    offset = len(SIGNATURE)
    method = raw_bytes[offset]
    offset += 1

    key_length = 32 if method == 1 else 16  # 1 = AES-256 (32 bytes), 0 = AES-128 (16 bytes)

    # Derive key
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=key_length,
        salt=JAVA_SALT,
        iterations=PBKDF2_ITERATIONS,
    )
    key = kdf.derive(password.encode("utf-8"))

    iv = raw_bytes[offset:offset + IV_LENGTH]
    offset += IV_LENGTH

    ciphertext = raw_bytes[offset:]

    # Decrypt AES-CBC
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    # Unpad PKCS7
    unpadder = PKCS7(128).unpadder()
    plaintext = unpadder.update(padded_plaintext) + unpadder.finalize()

    # Read contentType (4 bytes) and version (4 bytes)
    content_type, version = struct.unpack(">II", plaintext[:8])
    payload = plaintext[8:]

    # The payload is a ZIP stream containing data.portfolio or data.xml
    with zipfile.ZipFile(io.BytesIO(payload), "r") as zf:
        names = zf.namelist()
        if "data.portfolio" in names:
            extracted = zf.read("data.portfolio")
            return content_type, version, extracted
        elif "data.xml" in names:
            extracted = zf.read("data.xml")
            return content_type, version, extracted
        else:
            first_name = names[0]
            return content_type, version, zf.read(first_name)


def load_portfolio(file_path: str, password: Optional[str] = None) -> Dict[str, Any]:
    """
    Loads and normalizes a Portfolio Performance file (.portfolio or .xml).
    """
    with open(file_path, "rb") as f:
        data = f.read()

    # Check format
    if data.startswith(SIGNATURE):
        if not password:
            raise ValueError("File is password-protected AES encrypted. Password is required.")
        content_type, version, raw_payload = decrypt_portfolio_data(data, password)
        if content_type == 2:
            return parse_protobuf(raw_payload)
        else:
            return parse_xml(raw_payload.decode("utf-8"))
    elif data.startswith(b"PK\x03\x04"):
        # Unencrypted ZIP (Binary / Compressed)
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            names = zf.namelist()
            if "data.portfolio" in names:
                return parse_protobuf(zf.read("data.portfolio"))
            elif "data.xml" in names:
                return parse_xml(zf.read("data.xml").decode("utf-8"))
            else:
                return parse_protobuf(zf.read(names[0]))
    else:
        # Plain text XML
        try:
            return parse_xml(data.decode("utf-8"))
        except Exception:
            # Maybe UTF-8 with BOM or ISO-8859-1
            return parse_xml(data.decode("latin1"))


def parse_protobuf(raw_bytes: bytes) -> Dict[str, Any]:
    """Parses raw protobuf bytes using client_pb2."""
    if client_pb2 is None:
        raise ImportError("client_pb2 module not found. Compile client.proto first.")

    if raw_bytes.startswith(b"PPPBV1"):
        raw_bytes = raw_bytes[6:]

    client = client_pb2.PClient()
    client.ParseFromString(raw_bytes)

    securities = []
    sec_map = {}
    for s in client.securities:
        sec = {
            "uuid": s.uuid,
            "name": s.name,
            "isin": s.isin,
            "ticker": s.tickerSymbol,
            "currency": s.currencyCode,
            "wkn": s.wkn,
            "note": s.note,
            "is_retired": s.isRetired,
        }
        securities.append(sec)
        sec_map[s.uuid] = sec

    accounts = []
    acc_map = {}
    for a in client.accounts:
        acc = {
            "uuid": a.uuid,
            "name": a.name,
            "currency": a.currencyCode,
            "note": a.note,
            "is_retired": a.isRetired,
        }
        accounts.append(acc)
        acc_map[a.uuid] = acc

    portfolios = []
    port_map = {}
    for p in client.portfolios:
        port = {
            "uuid": p.uuid,
            "name": p.name,
            "note": p.note,
            "is_retired": p.isRetired,
            "reference_account": p.referenceAccount,
        }
        portfolios.append(port)
        port_map[p.uuid] = port

    # Transactions can be stored inside portfolios or client
    transactions = []
    # In client.proto, portfolios have transactions
    for p in client.portfolios:
        port_name = p.name
        # Check if transactions exist on portfolio or client
        # In proto, PPortfolio has transactions field or PClient has transactions
        if hasattr(p, "transactions"):
            for t in p.transactions:
                transactions.append(normalize_proto_tx(t, sec_map, port_name=port_name))

    # Also check client.transactions if present
    if hasattr(client, "transactions"):
        for t in client.transactions:
            transactions.append(normalize_proto_tx(t, sec_map))

    # Also check accounts transactions
    for a in client.accounts:
        if hasattr(a, "transactions"):
            for t in a.transactions:
                transactions.append(normalize_proto_tx(t, sec_map, acc_name=a.name))

    return {
        "version": client.version,
        "base_currency": client.baseCurrency,
        "securities": securities,
        "accounts": accounts,
        "portfolios": portfolios,
        "transactions": transactions,
    }


PROTO_TYPE_MAP = {
    0: "BUY",
    1: "SELL",
    2: "DELIVERY_INBOUND",
    3: "DELIVERY_OUTBOUND",
    4: "TRANSFER",
    5: "TRANSFER",
    6: "DEPOSIT",
    7: "REMOVAL",
    8: "DIVIDENDS",
    9: "INTEREST",
    10: "INTEREST_CHARGE",
    11: "TAXES",
    12: "TAX_REFUND",
    13: "FEES",
    14: "FEES_REFUND",
}


def normalize_proto_tx(t, sec_map, port_name=None, acc_name=None) -> Dict[str, Any]:
    # Decimal share conversion: PP stores shares as integer or PDecimalValue
    shares = 0.0
    if hasattr(t, "shares"):
        raw_shares = t.shares
        shares = float(raw_shares) / 100000000.0 if abs(raw_shares) > 1000000 else float(raw_shares)

    sec_uuid = t.security if hasattr(t, "security") else ""
    sec_info = sec_map.get(sec_uuid, {})

    t_type_val = getattr(t, "type", "")
    if isinstance(t_type_val, int):
        normalized_type = PROTO_TYPE_MAP.get(t_type_val, str(t_type_val))
    else:
        # String or enum name
        name_str = str(t_type_val).upper()
        if "SALE" in name_str:
            normalized_type = "SELL"
        elif "PURCHASE" in name_str:
            normalized_type = "BUY"
        elif "INBOUND" in name_str:
            normalized_type = "DELIVERY_INBOUND"
        elif "OUTBOUND" in name_str:
            normalized_type = "DELIVERY_OUTBOUND"
        elif "DIVIDEND" in name_str:
            normalized_type = "DIVIDENDS"
        else:
            normalized_type = name_str

    return {
        "uuid": getattr(t, "uuid", ""),
        "type": normalized_type,
        "date": str(getattr(t, "date", "")),
        "shares": shares,
        "amount": getattr(t, "amount", 0),
        "currency": getattr(t, "currencyCode", ""),
        "security_uuid": sec_uuid,
        "security_name": sec_info.get("name", "Unknown"),
        "isin": sec_info.get("isin", ""),
        "ticker": sec_info.get("ticker", ""),
        "note": getattr(t, "note", ""),
        "portfolio": port_name or getattr(t, "portfolio", ""),
        "account": acc_name or getattr(t, "account", ""),
    }


def parse_xml(xml_content: str) -> Dict[str, Any]:
    """Parses standard Portfolio Performance XML."""
    root = ET.fromstring(xml_content)

    base_currency = root.findtext("baseCurrency", "EUR")

    securities = []
    sec_map = {}
    for sec_el in root.findall(".//securities/security"):
        uuid = sec_el.findtext("uuid", "")
        name = sec_el.findtext("name", "")
        isin = sec_el.findtext("isin", "")
        ticker = sec_el.findtext("tickerSymbol", "")
        currency = sec_el.findtext("currencyCode", "")
        wkn = sec_el.findtext("wkn", "")
        note = sec_el.findtext("note", "")
        is_retired = sec_el.findtext("isRetired", "false").lower() == "true"

        sec = {
            "uuid": uuid,
            "name": name,
            "isin": isin,
            "ticker": ticker,
            "currency": currency,
            "wkn": wkn,
            "note": note,
            "is_retired": is_retired,
        }
        securities.append(sec)
        sec_map[uuid] = sec

    accounts = []
    for acc_el in root.findall(".//accounts/account"):
        accounts.append({
            "uuid": acc_el.findtext("uuid", ""),
            "name": acc_el.findtext("name", ""),
            "currency": acc_el.findtext("currencyCode", ""),
            "note": acc_el.findtext("note", ""),
            "is_retired": acc_el.findtext("isRetired", "false").lower() == "true",
        })

    portfolios = []
    for port_el in root.findall(".//portfolios/portfolio"):
        portfolios.append({
            "uuid": port_el.findtext("uuid", ""),
            "name": port_el.findtext("name", ""),
            "note": port_el.findtext("note", ""),
            "is_retired": port_el.findtext("isRetired", "false").lower() == "true",
        })

    transactions = []
    # Search all portfolio transactions
    for tx_el in root.findall(".//portfolio-transaction"):
        t_type = tx_el.findtext("type", "")
        date = tx_el.findtext("date", "")
        shares_str = tx_el.findtext("shares", "0")
        try:
            raw_shares = int(shares_str)
            shares = raw_shares / 100000000.0 if abs(raw_shares) > 1000000 else float(raw_shares)
        except ValueError:
            shares = float(shares_str)

        amount_str = tx_el.findtext("amount", "0")
        currency = tx_el.findtext("currencyCode", "")
        note = tx_el.findtext("note", "")

        # Security reference can be relative xpath or security tag
        sec_uuid = ""
        sec_name = "Unknown"
        isin = ""
        ticker = ""

        sec_ref = tx_el.find("security")
        if sec_ref is not None:
            # Check attribute reference or text
            ref = sec_ref.attrib.get("reference")
            if ref and ref.startswith("../"):
                # Handle xpath reference if possible
                pass
            uuid_el = sec_ref.find("uuid")
            if uuid_el is not None and uuid_el.text:
                sec_uuid = uuid_el.text
            elif sec_ref.text and sec_ref.text in sec_map:
                sec_uuid = sec_ref.text

        if sec_uuid in sec_map:
            sec_name = sec_map[sec_uuid]["name"]
            isin = sec_map[sec_uuid]["isin"]
            ticker = sec_map[sec_uuid]["ticker"]

        transactions.append({
            "uuid": tx_el.findtext("uuid", ""),
            "type": t_type,
            "date": date,
            "shares": shares,
            "amount": amount_str,
            "currency": currency,
            "security_uuid": sec_uuid,
            "security_name": sec_name,
            "isin": isin,
            "ticker": ticker,
            "note": note,
        })

    return {
        "base_currency": base_currency,
        "securities": securities,
        "accounts": accounts,
        "portfolios": portfolios,
        "transactions": transactions,
    }


def analyze_portfolio(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Performs comprehensive sanity checks on the parsed portfolio data:
    1. Negative share balance detection
    2. Ghost / corporate action securities detection
    3. Missing ISIN / ticker detection
    4. Account / portfolio reconciliation
    """
    securities = data.get("securities", [])
    accounts = data.get("accounts", [])
    portfolios = data.get("portfolios", [])
    transactions = data.get("transactions", [])

    # Map securities
    sec_by_uuid = {s["uuid"]: s for s in securities if s.get("uuid")}
    sec_by_name = {s["name"]: s for s in securities if s.get("name")}

    # Calculate share balances per security
    # Positive: BUY, DELIVERY_INBOUND
    # Negative: SELL, DELIVERY_OUTBOUND
    share_balances: Dict[str, float] = defaultdict(float)
    sec_txs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for tx in transactions:
        sec_name = tx.get("security_name", "Unknown")
        t_type = tx.get("type", "").upper()
        shares = tx.get("shares", 0.0)

        sec_txs[sec_name].append(tx)

        if t_type in ("BUY", "DELIVERY_INBOUND"):
            share_balances[sec_name] += abs(shares)
        elif t_type in ("SELL", "DELIVERY_OUTBOUND"):
            share_balances[sec_name] -= abs(shares)

    # 1. Negative share balances
    negative_holdings = []
    for sec_name, balance in share_balances.items():
        if balance < -0.0001:  # small epsilon for floating point
            sec = sec_by_name.get(sec_name, {})
            negative_holdings.append({
                "security_name": sec_name,
                "isin": sec.get("isin", ""),
                "ticker": sec.get("ticker", ""),
                "shares_holding": round(balance, 4),
                "transactions_count": len(sec_txs[sec_name]),
                "first_date": min((t["date"] for t in sec_txs[sec_name] if t.get("date")), default=""),
                "last_date": max((t["date"] for t in sec_txs[sec_name] if t.get("date")), default=""),
                "reason": "Sold or delivered outbound before initial purchase was recorded (likely pre-2022 buy lot)",
            })

    # Sort negative holdings by worst deficit
    negative_holdings.sort(key=lambda x: x["shares_holding"])

    # 2. Ghost securities (e.g. CASH DIVIDEND or SPLIT in name)
    ghost_securities = []
    ghost_keywords = [
        "CASH DIVIDEND", "DIVIDEND", "TAX", "SPLIT", "REVERSE SPLIT",
        "SPINOFF", "SPIN-OFF", "RIGHTS ISSUE", "FRACTIONAL", "OLD"
    ]

    for sec in securities:
        name_upper = sec["name"].upper()
        is_ghost = False
        reason = ""

        for kw in ghost_keywords:
            if kw in name_upper and not any(legit in name_upper for legit in ["DIVIDEND ETF", "DIVIDEND ARISTOCRATS", "GLOBAL DIVIDEND"]):
                is_ghost = True
                reason = f"Contains corporate action/cash transaction keyword '{kw}' in security name"
                break

        if is_ghost:
            tx_count = len(sec_txs.get(sec["name"], []))
            ghost_securities.append({
                "security_name": sec["name"],
                "isin": sec.get("isin", ""),
                "ticker": sec.get("ticker", ""),
                "currency": sec.get("currency", ""),
                "transactions_count": tx_count,
                "reason": reason,
                "action_recommended": (
                    "Re-assign transactions to the parent security (e.g. via Edit transaction -> Security dropdown), "
                    "then delete this ghost security once transaction count is 0."
                ),
            })

    # 3. Missing ISIN securities
    missing_isin = []
    for sec in securities:
        if not sec.get("isin") and not sec.get("is_retired"):
            missing_isin.append({
                "security_name": sec["name"],
                "ticker": sec.get("ticker", ""),
                "currency": sec.get("currency", ""),
            })

    return {
        "total_securities": len(securities),
        "total_accounts": len(accounts),
        "total_portfolios": len(portfolios),
        "total_transactions": len(transactions),
        "negative_holdings": negative_holdings,
        "ghost_securities": ghost_securities,
        "missing_isin_count": len(missing_isin),
        "missing_isin": missing_isin[:10],
    }


def main():
    parser = argparse.ArgumentParser(description="Portfolio Performance File Sanity Checker")
    parser.add_argument("file", help="Path to .portfolio or .xml file")
    parser.add_argument("--password", "-p", help="Password for AES encrypted .portfolio file")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    password = args.password or os.environ.get("PP_PASSWORD")

    try:
        data = load_portfolio(args.file, password)
        report = analyze_portfolio(data)

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("=" * 60)
            print("  PORTFOLIO PERFORMANCE SANITY CHECK REPORT")
            print("=" * 60)
            print(f"Total Securities:    {report['total_securities']}")
            print(f"Total Accounts:      {report['total_accounts']}")
            print(f"Total Portfolios:    {report['total_portfolios']}")
            print(f"Total Transactions:  {report['total_transactions']}")
            print()

            print(f"[-] Negative Holding Positions: {len(report['negative_holdings'])}")
            for item in report['negative_holdings']:
                print(f"  • {item['security_name']} ({item['isin']}): {item['shares_holding']} shares")
                print(f"    Transactions: {item['transactions_count']} | Range: {item['first_date']} -> {item['last_date']}")
            print()

            print(f"[-] Ghost / Corporate Action Securities: {len(report['ghost_securities'])}")
            for item in report['ghost_securities']:
                print(f"  • {item['security_name']}")
                print(f"    Reason: {item['reason']}")
                print(f"    Recommendation: {item['action_recommended']}")
            print()

            print(f"[-] Securities without ISIN: {report['missing_isin_count']}")
            for item in report['missing_isin']:
                print(f"  • {item['security_name']} ({item['currency']})")

    except Exception as e:
        print(f"Error analyzing portfolio: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
