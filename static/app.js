// Pequenos comportamentos: envio automático de selects, confirmações e impressão.
document.documentElement.classList.add("com-js");

document.addEventListener("change", (ev) => {
  const campo = ev.target.closest("select[data-autoenviar]");
  if (campo && campo.form) campo.form.requestSubmit();
});

document.addEventListener("submit", (ev) => {
  const pergunta = ev.target.dataset.confirmar;
  if (pergunta && !window.confirm(pergunta)) ev.preventDefault();
});

document.addEventListener("click", (ev) => {
  if (ev.target.closest("[data-imprimir]")) window.print();
});

// Caixa "marcar todas" de uma tabela: data-marcar-todos="<nome dos checkboxes>".
document.addEventListener("change", (ev) => {
  const todos = ev.target.closest("[data-marcar-todos]");
  if (!todos) return;
  todos.form.querySelectorAll(`input[name="${todos.dataset.marcarTodos}"]`)
    .forEach((caixa) => { caixa.checked = todos.checked; });
});
