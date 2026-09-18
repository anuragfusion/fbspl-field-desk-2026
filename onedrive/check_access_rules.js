/* Guards the four access rules that are easy to break by editing the markup.
   Run: node check_access_rules.js   (exits non-zero on failure) */
var fs=require("fs"), assert=require("assert");
var h=fs.readFileSync("FBSPL_Field_Desk_AppliedNet2026.html","utf8");

function card(title){
  var i=h.indexOf(">"+title+"</h3>");
  assert.ok(i>0, "card not found: "+title);
  return h.slice(h.lastIndexOf('<div class="card',i), i);
}
assert.ok(/class="card adminonly"/.test(card("Publish to the floor team")), "Publish card must be admin-only");
assert.ok(/class="card adminonly"/.test(card("Team accounts")),             "Team accounts must be admin-only");
assert.ok(!/adminonly/.test(card("Change my password")),                    "Floor team must be able to change their own password");
assert.ok(!/adminonly/.test(card("Receive from the office")),               "Floor team must be able to import a pack");
assert.ok(/function exportPack\([^)]*\)\{[\s\S]{0,300}?me\.role==="admin"/.test(h), "exportPack must guard on role");
assert.ok(/setMode\(admin && prefs\(\)\.lastMode!=="view"/.test(h),         "Admins must land in Admin mode by default");
console.log("access rules ok");
