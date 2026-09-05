
function tick(){document.getElementById('clock').textContent=new Date().toLocaleTimeString('en-IN',{hour12:true})}tick();setInterval(tick,1000);let editMode=false;const $=id=>document.getElementById(id);
function openAdd(){editMode=false;$('modalTitle').textContent='Add Portfolio Position';$('fId').value='';$('fPid').value='(Auto generated)';$('fPid').style.display='none';$('fClient').value='{{ client.client_id }}';$('fSymbol').value='';$('fProduct').value='NORMAL';$('fQty').value='';$('fBuy').value='';$('fExchange').value='BSE';$('positionModal').classList.add('show')}
function openEdit(btn){editMode=true;const r=btn.closest('tr');$('fId').value=r.dataset.id||'';$('fPid').value=r.dataset.portfolioId||r.dataset.id||'-';$('fPid').style.display='block';$('fClient').value=r.cells[1].innerText.trim();$('fSymbol').value=r.cells[2].innerText.trim();$('fProduct').value=r.cells[3].innerText.trim();$('fExchange').value=r.cells[4].innerText.trim();$('fQty').value=r.cells[7].innerText.trim();$('fBuy').value=r.cells[8].innerText.trim();$('modalTitle').textContent='Modify Portfolio Position';$('positionModal').classList.add('show')}
function closeModal(){$('positionModal').classList.remove('show')}
async function savePosition(){const payload={client_id:$('fClient').value.trim(),symbol:$('fSymbol').value.trim().toUpperCase(),product:$('fProduct').value.trim().toUpperCase(),exchange:$('fExchange').value,quantity:Number($('fQty').value),buy_price:Number($('fBuy').value)};if(!payload.symbol||payload.quantity<=0||payload.buy_price<=0){alert('Please enter Symbol, positive Qty and Buy Price.');return}if(editMode)payload.id=$('fId').value;const res=await fetch(editMode?'/api/holdings/modify':'/api/holdings/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await res.json();if(!data.success){alert(data.message||data.error||'Operation failed');return}location.reload()}
async function deleteHolding(btn){if(!confirm('Delete this portfolio record?'))return;const r=btn.closest('tr');const id=r.dataset.id||r.dataset.portfolioId;const res=await fetch('/api/holdings/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,client_id:$('fClient').value})});const data=await res.json();if(!data.success){alert(data.message||data.error||'Delete failed');return}location.reload()}
async function bulkModifySelected(){const rows=[...document.querySelectorAll('.row-check:checked')];if(!rows.length){alert('Select at least one portfolio row.');return}$('bSymbol').value='';$('bProduct').value='';$('bQty').value='';$('bBuy').value='';$('bExchange').value='BSE';$('bulkModal').classList.add('show')}
async function applyBulkModify(){const rows=[...document.querySelectorAll('.row-check:checked')];const qty=Number($('bQty').value),buy=Number($('bBuy').value),symbol=$('bSymbol').value.trim().toUpperCase(),product=$('bProduct').value.trim().toUpperCase();if(qty<=0||buy<=0){alert('Positive Quantity and Buy Price are required.');return}if(!confirm(`Modify ${rows.length} selected position(s)?`))return;for(const r of rows){const payload={id:r.dataset.id||r.dataset.portfolioId,client_id:$('fClient').value.trim(),quantity:qty,buy_price:buy,exchange:$('bExchange').value};if(symbol)payload.symbol=symbol;if(product)payload.product=product;const res=await fetch('/api/holdings/modify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await res.json();if(!data.success){alert(data.message||data.error||'Bulk modify failed');return}}location.reload()}
async function bulkDeleteSelected(){const rows=[...document.querySelectorAll('.row-check:checked')];if(!rows.length){alert('Select at least one portfolio row.');return}if(!confirm(`Delete ${rows.length} selected position(s)? This cannot be undone.`))return;for(const r of rows){const id=r.dataset.id||r.dataset.portfolioId;const res=await fetch('/api/holdings/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,client_id:$('fClient').value.trim()})});const data=await res.json();if(!data.success){alert(data.message||data.error||'Bulk delete failed');return}}location.reload()}
function toggleAll(m){document.querySelectorAll('.row-check').forEach(x=>x.checked=m.checked);countSelected()}function selectAllRows(){document.querySelectorAll('.row-check').forEach(x=>x.checked=true);countSelected()}function filterClientPortfolio(){
  const symbol=($('clientSymbolSearch').value||'').trim().toUpperCase();
  const product=($('clientProductSearch').value||'').trim().toUpperCase();
  const rows=[...document.querySelectorAll('#portfolioTable tbody tr')];
  let visible=0;
  rows.forEach(r=>{
    if(!r.querySelector('.row-check')) return;
    const rowSymbol=(r.cells[2]?.innerText||'').trim().toUpperCase();
    const rowProduct=(r.cells[3]?.innerText||'').trim().toUpperCase();
    const okSymbol=!symbol || rowSymbol.includes(symbol);
    const okProduct=!product || rowProduct===product;
    r.style.display=(okSymbol && okProduct)?'':'none';
    if(okSymbol && okProduct) visible++;
  });
  clearRows();
  const pill=document.getElementById('clientVisibleCount');
  if(pill) pill.textContent=visible+' visible';
}
function clearClientSearch(){
  $('clientSymbolSearch').value='';
  $('clientProductSearch').value='';
  document.querySelectorAll('#portfolioTable tbody tr').forEach(r=>{ if(r.querySelector('.row-check')) r.style.display=''; });
  clearRows();
  const pill=document.getElementById('clientVisibleCount');
  if(pill) pill.textContent=document.querySelectorAll('#portfolioTable tbody .row-check').length+' visible';
}
function clearRows(){document.querySelectorAll('.row-check').forEach(x=>x.checked=false);countSelected()}function countSelected(){$('selectedCount').textContent=document.querySelectorAll('.row-check:checked').length+' selected'}function esc(v){return '"'+String(v??'').replaceAll('"','""')+'"'}function downloadSelected(){const rows=[...document.querySelectorAll('.row-check:checked')].map(x=>x.closest('tr'));if(!rows.length){alert('Select at least one row.');return}const h=['Client ID','Symbol','Product','Exchange','MTM','LTP','Qty','Buy Price','Change','Portfolio ID'];const out=[h.map(esc).join(',')];rows.forEach(r=>out.push([r.cells[1].innerText,r.cells[2].innerText,r.cells[3].innerText,r.cells[4].innerText,r.cells[5].innerText,r.cells[6].innerText,r.cells[7].innerText,r.cells[8].innerText,r.cells[9].innerText,r.dataset.id||r.dataset.portfolioId].map(esc).join(',')));const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([out.join('\n')],{type:'text/csv'}));a.download='portfolio_selected.csv';a.click()}
document.getElementById('clientSymbolSearch').addEventListener('keydown',e=>{if(e.key==='Enter')filterClientPortfolio()});document.getElementById('clientProductSearch').addEventListener('change',filterClientPortfolio);
