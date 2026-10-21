import argparse
import os
import signal
from .config import Settings

def main():
    parser=argparse.ArgumentParser(description='Local single-image H3 V2 compatible service; never starts cloud resources.')
    sub=parser.add_subparsers(dest='command',required=True)
    api=sub.add_parser('api',help='Run the lightweight HTTP control plane')
    api.add_argument('--host',default='127.0.0.1')
    api.add_argument('--port',type=int,default=30010)
    worker=sub.add_parser('worker',help='Run one serial CPU generation worker')
    worker.add_argument('--once',action='store_true')
    args=parser.parse_args()
    settings=Settings.from_env()
    os.umask(0o077)
    if args.command=='api':
        if not 1<=args.port<=65535:parser.error('port must be between 1 and 65535')
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(settings),host=args.host,port=args.port,access_log=False)
    else:
        from .worker import Worker
        with Worker(settings) as w:
            def stop(signum,frame):w.stop.set()
            signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
            if args.once:w.run_once()
            else:w.run_forever()

if __name__=='__main__':main()
