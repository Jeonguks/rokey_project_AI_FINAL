function showSnackbar(text) {
  snackbar.textContent = text;
  snackbar.style.display = "block";

  setTimeout(() => {
    snackbar.style.display = "none";
  }, 3000);
}


const es = new EventSource("/events");

es.addEventListener("detection", (e) => {
  const ev = JSON.parse(e.data);
//   console.log("DETECTION EVENT:", ev);
  showSnackbar(`${ev.camera} 에서 ${ev.label} 감지됨`);
//   alert(`${ev.camera} 에서 ${ev.label} 감지됨`);
});
