"use strict";

const card = document.querySelector("#card");
const bankLabel = document.querySelector("#bank-name");

if (card && bankLabel) {
  const showBank = () => {
    const prefix = card.value.slice(0, 4);
    if (!/^[0-9]{4}$/.test(prefix)) {
      bankLabel.textContent = "Банк: введите первые четыре цифры";
      return;
    }
    const p = Number(prefix);
    const bank = p <= 3333 ? "ХраниБанк" : p <= 6666 ? "Ы-Банк" : "БКБ";
    bankLabel.textContent = `Банк: ${bank}`;
  };
  card.addEventListener("input", showBank);
  showBank();
}

const marked = document.querySelector("#marked");
const method = document.querySelector("#method");
const qty = document.querySelector("#qty");
const unit = document.querySelector("#unit");
const markField = document.querySelector("#mark-field");
const markCode = document.querySelector("#mark-code");

if (marked && method && qty && unit && markField && markCode) {
  const updateItem = () => {
    const isMarked = marked.checked;
    if (isMarked) {
      qty.value = "1";
      unit.value = "0";
    }
    qty.readOnly = isMarked;
    unit.disabled = isMarked;
    const needCode = isMarked && method.value === "4";
    markField.hidden = !needCode;
    markCode.disabled = !needCode;
    markCode.required = needCode;
  };
  marked.addEventListener("change", updateItem);
  method.addEventListener("change", updateItem);
  updateItem();
}