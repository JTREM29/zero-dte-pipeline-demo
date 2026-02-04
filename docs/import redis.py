import redis
import json
import uuid
import time

def get_redis(host='192.168.1.10', port=6379):
    return redis.Redis(host=host, port=port, decode_responses=True)

def enqueue_render(symbol='SPY', interval='1m', duration=10, redis_host='192.168.1.10'):
    r = get_redis(host=redis_host)
    job_id = str(uuid.uuid4())
    payload = {
        'id': job_id,
        'type': 'render',
        'payload': {
            'symbol': symbol,
            'interval': interval,
            'duration': duration
        }
    }
    r.lpush('jobs:render', json.dumps(payload))
    print(f"Enqueued render job {job_id}")
    return job_id

if __name__ == '__main__':
    enqueue_render(symbol='SPY', interval='1m', duration=10)
    time.sleep(0.5)