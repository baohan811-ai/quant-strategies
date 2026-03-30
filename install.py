from WindPy import *

ret = w.start()
print(ret)

ret = w.isconnected()
print(ret)

#test WSS function
ret = w.wss("000001.SZ", "sec_name","")
print(ret)