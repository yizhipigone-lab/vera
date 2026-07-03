"""Fix batch_fixed.py stubs to return proper types."""
with open('batch_fixed.py','r',encoding='utf-8')as f:content=f.read()

# 1. Add missing stubs
extra='''
    'HYBLOCK':lambda p:pd.DataFrame(False,index=C.index,columns=C.columns),
    'DYBLOCK':lambda p:pd.DataFrame(False,index=C.index,columns=C.columns),
    'GNBLOCK':lambda p:pd.DataFrame(False,index=C.index,columns=C.columns),
    'SUMBARS':lambda x,n:pd.DataFrame(0.0,index=C.index,columns=C.columns),
    'DRAWNULL':lambda:pd.DataFrame(np.nan,index=C.index,columns=C.columns),
    'TROUGHBARS':lambda x,n,m:pd.DataFrame(0.0,index=C.index,columns=C.columns),
    'PEAKBARS':lambda x,n,m:pd.DataFrame(0.0,index=C.index,columns=C.columns),
    'CONST':lambda x:float(x.iloc[-1])if isinstance(x,pd.DataFrame)else float(x),
    'CURRBARSCOUNT':lambda:len(C.index),
    'TOTALBARSCOUNT':lambda:len(C.index),
    'ISLASTBAR':lambda:pd.DataFrame(False,index=C.index,columns=C.columns),
    'PERIOD':lambda:5,
    'DATATYPE':lambda:6,
    'ALIGNRIGHT':lambda:pd.DataFrame(False,index=C.index,columns=C.columns),
    'BARTIME':lambda:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'DTPRICE':lambda:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'PARTLINE':lambda x,cond:x,
    'VERTLINE':lambda *a,**k:pd.DataFrame(False,index=C.index,columns=C.columns),
    'DRAWGBK':lambda *a,**k:pd.DataFrame(False,index=C.index,columns=C.columns),
''';
insert_pos=content.find("'K':C,")
if insert_pos>0:
    content=content[:insert_pos]+extra+content[insert_pos:]

# 2. Fix LINETHICK residue: add broader strip pattern
old_pat="for pat in[r',\s*COLOR"
new_pat="for pat in[r',?\s*LINETHICK\d*',r',\s*COLOR"
content=content.replace(old_pat,new_pat)

# 3. Fix leading-zero regex (can break '00' codes)
old_lz="line=re.sub(r'(?<!\w)0+(\d+)(?!\w)',r'\1',line)"
new_lz="# (leading-zero fix removed to preserve codes like 00/30/60)"
content=content.replace(old_lz,new_lz)

# 4. Fix: COLORXXXX at end of lines (without comma prefix)
# Add a broader strip
old_colstrip="line=re.sub(r',\s*COLORSTICK','',line,flags=re.IGNORECASE)"
new_colstrip="line=re.sub(r',\s*COLORSTICK','',line,flags=re.IGNORECASE)\n        line=re.sub(r'\s+COLOR\w*','',line,flags=re.IGNORECASE)\n        line=re.sub(r'\s+LINETHICK\d*','',line,flags=re.IGNORECASE)"
content=content.replace(old_colstrip,new_colstrip)

with open('batch_fixed.py','w',encoding='utf-8')as f:
    f.write(content)
print('Fixed batch_fixed.py')
