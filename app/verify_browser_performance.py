#!/usr/bin/env python3
"""Check indexed selection behavior. This does not measure visible paint latency."""
import argparse
from verify_frontend import main as verify_native_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='verify', choices=['verify'])
    parser.parse_args()
    verify_native_model()


if __name__ == '__main__':
    main()
