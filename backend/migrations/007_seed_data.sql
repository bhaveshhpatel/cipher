-- Migration 007: Seed data for flow_events
-- All NOT NULL columns included: dte, fill_price, bid, ask, size,
-- bid_ask_class, is_aggressive, is_golden_sweep, exchange_count,
-- fill_count, open_interest, iv, underlying_price

INSERT INTO public.flow_events
  (ticker, contract_type, strike, expiry, dte, fill_price,
   bid, ask, size, premium, trade_type, bid_ask_class,
   is_aggressive, is_golden_sweep, sentiment, influence_tier,
   conviction_score, exchange_count, fill_count,
   open_interest, iv, underlying_price, created_at)
VALUES
  ('AAPL','CALL',195.00,'2025-05-16',22,2.85, 2.80,2.90,1000,2850000,'SWEEP', 'ASK',true, true, 'BULLISH','WHALE',        0.91,3,4,45000,0.32,192.50,now()-interval '1 minute'),
  ('AAPL','CALL',200.00,'2025-06-20',57,1.20, 1.18,1.22,1000,1200000,'BLOCK', 'MID',false,false,'BULLISH','INSTITUTIONAL',0.78,2,2,38000,0.29,192.50,now()-interval '3 minutes'),
  ('TSLA','PUT', 175.00,'2025-05-09',15,3.40, 3.35,3.45,1000,3400000,'SWEEP', 'ASK',true, true, 'BEARISH','WHALE',        0.88,4,5,52000,0.48,182.30,now()-interval '5 minutes'),
  ('NVDA','CALL',950.00,'2025-05-16',22,4.10, 4.05,4.15,1000,4100000,'SWEEP', 'ASK',true, true, 'BULLISH','WHALE',        0.95,5,6,61000,0.38,940.00,now()-interval '7 minutes'),
  ('SPY', 'PUT', 520.00,'2025-05-02', 8,9.80, 9.75,9.85,1000,9800000,'BLOCK', 'BID',false,false,'BEARISH','INSTITUTIONAL',0.82,2,3,95000,0.18,524.50,now()-interval '9 minutes'),
  ('QQQ', 'CALL',450.00,'2025-05-09',15,2.20, 2.17,2.23, 500,2200000,'SPLIT', 'MID',false,false,'BULLISH','LARGE',        0.67,2,2,42000,0.22,447.80,now()-interval '11 minutes'),
  ('META','CALL',580.00,'2025-05-16',22,1.75, 1.72,1.78,1000,1750000,'SWEEP', 'ASK',true, false,'BULLISH','INSTITUTIONAL',0.79,3,3,28000,0.27,573.20,now()-interval '13 minutes'),
  ('AMZN','CALL',205.00,'2025-06-20',57,3.30, 3.27,3.33,1000,3300000,'BLOCK', 'MID',false,false,'BULLISH','WHALE',        0.85,2,2,35000,0.25,201.40,now()-interval '15 minutes'),
  ('MSFT','PUT', 410.00,'2025-05-09',15,1.10, 1.08,1.12, 500,1100000,'SINGLE','BID',false,false,'BEARISH','LARGE',        0.55,1,1,22000,0.20,415.60,now()-interval '17 minutes'),
  ('GOOG','CALL',180.00,'2025-05-16',22,2.60, 2.57,2.63,1000,2600000,'SWEEP', 'ASK',true, true, 'BULLISH','WHALE',        0.90,4,5,18000,0.26,176.90,now()-interval '19 minutes'),
  ('TSLA','CALL',190.00,'2025-06-20',57,1.90, 1.87,1.93,1000,1900000,'BLOCK', 'MID',false,false,'BULLISH','INSTITUTIONAL',0.72,2,2,48000,0.45,182.30,now()-interval '21 minutes'),
  ('NVDA','PUT', 870.00,'2025-05-02', 8,2.10, 2.07,2.13, 500,2100000,'SPLIT', 'MID',false,false,'BEARISH','LARGE',        0.60,2,2,39000,0.40,940.00,now()-interval '23 minutes'),
  ('AAPL','PUT', 185.00,'2025-05-09',15,0.89, 0.87,0.91, 200, 890000,'SINGLE','BID',false,false,'BEARISH','RETAIL',       0.42,1,1,31000,0.30,192.50,now()-interval '25 minutes'),
  ('SPY', 'CALL',530.00,'2025-05-16',22,5.50, 5.47,5.53,1000,5500000,'SWEEP', 'ASK',true, true, 'BULLISH','WHALE',        0.93,5,6,88000,0.17,524.50,now()-interval '27 minutes'),
  ('AMD', 'CALL',175.00,'2025-05-09',15,1.45, 1.43,1.47,1000,1450000,'BLOCK', 'MID',false,false,'BULLISH','INSTITUTIONAL',0.76,2,2,33000,0.35,168.70,now()-interval '29 minutes'),
  ('COIN','CALL',245.00,'2025-05-16',22,3.20, 3.17,3.23,1000,3200000,'SWEEP', 'ASK',true, true, 'BULLISH','WHALE',        0.87,4,5,24000,0.62,238.50,now()-interval '31 minutes'),
  ('PLTR','CALL', 28.00,'2025-06-20',57,0.78, 0.76,0.80, 500, 780000,'BLOCK', 'MID',false,false,'BULLISH','LARGE',        0.65,2,2,95000,0.55, 26.40,now()-interval '33 minutes'),
  ('MSTR','CALL',420.00,'2025-05-09',15,2.90, 2.87,2.93,1000,2900000,'SWEEP', 'ASK',true, true, 'BULLISH','WHALE',        0.89,4,5,12000,0.70,408.00,now()-interval '35 minutes'),
  ('QQQ', 'PUT', 440.00,'2025-05-02', 8,4.20, 4.17,4.23,1000,4200000,'BLOCK', 'BID',false,false,'BEARISH','INSTITUTIONAL',0.80,3,3,55000,0.23,447.80,now()-interval '37 minutes'),
  ('AMZN','PUT', 195.00,'2025-05-16',22,1.60, 1.58,1.62, 500,1600000,'SPLIT', 'MID',false,false,'BEARISH','LARGE',        0.58,2,2,27000,0.26,201.40,now()-interval '39 minutes');
