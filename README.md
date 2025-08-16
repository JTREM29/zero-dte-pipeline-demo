# zero-dte-pipeline-demo
A demonstration repository showcasing parts of a live 0DTE options trading pipeline integrating Polygon.io, Alpha Vantage, and (optionally) TrendSpider APIs. Includes example feature engineering, delta-bucket bias logic, and risk gating.
import pandas as pd
import numpy as np

def delta_bucket_bias(options_data, target_delta=0.25):
    # options_data expected to have columns: ['strike', 'delta', 'iv', 'type']
    calls = options_data[options_data['type'] == 'call']
    puts = options_data[options_data['type'] == 'put']
    call_near = calls.iloc[(calls['delta'] - target_delta).abs().argsort()[:1]]
    put_near = puts.iloc[(puts['delta'] + target_delta).abs().argsort()[:1]]
    skew = call_near['iv'].mean() - put_near['iv'].mean()
    bias = 'bullish' if skew < 0 else 'bearish'
    return bias, skew
