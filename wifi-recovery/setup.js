"use strict";

const password = document.getElementById("password");
const reveal = document.querySelector(".reveal");
reveal.hidden = false;
reveal.addEventListener("click", () => {
    const visible = password.type === "password";
    password.type = visible ? "text" : "password";
    reveal.setAttribute("aria-label", visible ? "Hide password" : "Show password");
    reveal.setAttribute("aria-pressed", String(visible));
});

const network = document.getElementById("ssid");
if (network.tagName === "SELECT") {
    const updatePassword = () => {
        const selected = network.selectedOptions[0];
        const open = selected && selected.dataset.secure === "false";
        document.getElementById("password-field").hidden = open;
        document.getElementById("open-network").hidden = !open;
        password.disabled = open;
        password.required = Boolean(network.value) && !open;
        password.minLength = open ? 0 : 8;
        if (open) password.value = "";
    };
    network.addEventListener("change", updatePassword);
    updatePassword();
}
