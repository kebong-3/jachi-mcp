"""Deployment entry point. --http for Streamable HTTP, default for stdio."""
import argparse
from jachi.server import start

def main():
    parser=argparse.ArgumentParser(description='조례검토·입법지원 MCP 2.0')
    parser.add_argument('--http',action='store_true')
    args=parser.parse_args()
    start(args.http)

if __name__=='__main__':main()
