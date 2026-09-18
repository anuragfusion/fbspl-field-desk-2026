// hashPass() copied verbatim from FBSPL_Field_Desk_AppliedNet2026.html
function hashPass(user,pass){
  var s="fbspl:an26:"+String(user||"").toLowerCase()+":"+String(pass||""), h1=5381,h2=52711,i,c,r;
  for(r=0;r<1200;r++){
    for(i=0;i<s.length;i++){ c=s.charCodeAt(i); h1=((h1<<5)+h1)^c; h2=((h2<<5)+h2)^(c+r); }
    h1=h1>>>0; h2=h2>>>0;
  }
  return "h1:"+(h1>>>0).toString(36)+(h2>>>0).toString(36);
}
const cases=[["admin","FieldDesk2026"],["priya","harbor-4821"],["Ankit","p@ssw0rd!"],
             ["admin",""],["x","ünïcodé-123"],["UPPER","MiXeD-Case-9"]];
console.log(JSON.stringify(cases.map(([u,p])=>({user:u,pass:p,hash:hashPass(u,p)}))));
