"""Protocol-aware, non-mutating local service readiness probes."""
import argparse
import smtplib
import sys


def smtp_ready(host, port, timeout=30):
    # SMTP() waits for the server's 220 greeting before sending any commands.
    # Writing EHLO immediately on TCP connect triggers Haraka's early_talker guard.
    try:
        with smtplib.SMTP(host=host, port=port, local_hostname='localhost.localdomain', timeout=timeout) as client:
            code, _ = client.noop()
            return code == 250
    except (OSError, smtplib.SMTPException):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('protocol', choices=['smtp'])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    ready = smtp_ready(args.host, args.port, args.timeout)
    print(f'{args.protocol}:{args.port} ' + ('ready' if ready else 'not ready'))
    return 0 if ready else 1


if __name__ == '__main__':
    sys.exit(main())
