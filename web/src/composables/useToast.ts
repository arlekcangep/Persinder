import { readonly, ref } from "vue";

const message = ref("");
let timer: number | undefined;

function show(text: string): void {
  message.value = text;
  if (timer !== undefined) window.clearTimeout(timer);
  timer = window.setTimeout(() => {
    message.value = "";
    timer = undefined;
  }, 3200);
}

export function useToast() {
  return { message: readonly(message), show };
}
