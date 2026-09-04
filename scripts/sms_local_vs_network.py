#!/usr/bin/env python3
"""Separate a local SMS problem from a network refusal.

AT+CMGW writes a message into the modem's own store using exactly the same text
encoding path as AT+CMGS, but never touches the network. If CMGW succeeds and
CMGS fails, the encoding and storage are fine and the network is refusing the
submission. AT+CMSS then re-sends the stored message, which some networks accept
when a direct CMGS is rejected.
"""
import re
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = modem.normalise_number(sys.argv[1] if len(sys.argv) > 1 else "0771324635")


def main():
    m = modem.Modem()
    m.open()
    for cmd in ["ATE0", "AT+CMEE=2", "AT+CMGF=1", 'AT+CSCS="GSM"']:
        m.send_raw(cmd)

    print("target:", TARGET)

    print()
    print("== message service level ==")
    for cmd in ["AT+CSMS?", "AT+CSMS=?", "AT+CSMS=1", "AT+CSMS?"]:
        print("   %-14s %s" % (cmd, (m.send_raw(cmd, timeout=8)[1] or "")
                               .replace("\n", " | ")))

    print()
    print("== AT+CMGW: write to modem store (no network involved) ==")
    index = None
    try:
        with m._cmd_lock:
            m.send('AT+CMGW="%s"' % TARGET, timeout=15, collect_prompt=True)
            m._in_command.set()
            try:
                m._write_payload(b"local store test\x1a")
                lines = m._collect("AT+CMGW payload", 30, False)
            finally:
                m._in_command.clear()
        print("   response:", lines)
        for line in lines:
            mm = re.search(r"\+CMGW:\s*(\d+)", line)
            if mm:
                index = int(mm.group(1))
        print("   WROTE OK at index", index)
    except Exception as exc:
        print("   CMGW FAILED ->", exc)

    if index is not None:
        print()
        print("== AT+CMSS: submit the stored message ==")
        ok, txt = m.send_raw("AT+CMSS=%d" % index, timeout=60)
        print("   ok=%s %s" % (ok, (txt or "").replace("\n", " | ")))
        if not ok:
            print("   AT+CEER ->", (m.send_raw("AT+CEER")[1] or "")
                  .replace("\n", " | "))

        print()
        print("== stored messages ==")
        for msg in m.read_messages("ALL"):
            print("   idx=%-3s %-6s %-15s %s" % (
                msg.get("index"), msg.get("status"), msg.get("number"),
                (msg.get("body") or "")[:40]))

        print()
        print("== cleaning up the test message ==")
        try:
            m.delete_message(index)
            print("   deleted index", index)
        except Exception as exc:
            print("   delete failed:", exc)

    print()
    print("== network feature support ==")
    for cmd in ["AT+CSSN=1,1", "AT+CUSD=?", "AT+CMGS=?", "AT+CSCB?",
                'AT+QCFG="servicedomain"']:
        print("   %-26s %s" % (cmd, (m.send_raw(cmd, timeout=8)[1] or "")
                               .replace("\n", " | ")[:80]))

    print()
    print("DONE")
    m.close()


if __name__ == "__main__":
    main()
