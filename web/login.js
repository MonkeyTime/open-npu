import {api, handleForm} from "./portal.js";

handleForm(document.getElementById("login-form"), document.getElementById("login-result"), async (data) => {
  await api("/admin/session", data);
  location.assign("/admin/access");
});
