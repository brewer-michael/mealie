<template>
  <div
    class="vue-rectangle-stencil ingest-region-stencil"
    :class="{
      'vue-rectangle-stencil--movable': movable,
      'vue-rectangle-stencil--moving': moving,
      'vue-rectangle-stencil--resizing': resizing,
    }"
    :style="style"
    tabindex="0"
    role="group"
    :aria-label="label || undefined"
    :aria-describedby="describedBy || undefined"
  >
    <BoundingBox
      class="vue-rectangle-stencil__bounding-box"
      :width="stencilCoordinates.width"
      :height="stencilCoordinates.height"
      :transitions="transitions"
      :resizable="resizable"
      @resize="onResize"
      @resize-end="onResizeEnd"
    >
      <AnchoredDraggableArea :movable="movable" @move="onMove" @move-end="onMoveEnd">
        <StencilPreview
          class="vue-rectangle-stencil__preview"
          :image="image"
          :coordinates="coordinates"
          :width="stencilCoordinates.width"
          :height="stencilCoordinates.height"
          :transitions="transitions"
        />
      </AnchoredDraggableArea>
    </BoundingBox>
  </div>
</template>

<script lang="ts">
import { computed, defineComponent, ref } from "vue";
import { BoundingBox, DraggableArea, StencilPreview } from "vue-advanced-cropper";

/** What `DraggableArea` keeps between its touch handlers (its own data, which the anchored version sets) */
interface DraggableAreaState {
  movable: boolean;
  touches: Touch[];
  touchStarted: boolean;
  initAnchor: (point: { clientX: number; clientY: number }) => void;
}

/**
 * `DraggableArea`, anchored where the finger lands. The library's own waits until the finger has travelled 20 px
 * (`activationDistance`) and anchors there, so a touch drag left the box 20 px behind the finger.
 */
const AnchoredDraggableArea = defineComponent({
  name: "IngestAnchoredDraggableArea",
  extends: DraggableArea,
  methods: {
    onTouchStart(event: TouchEvent) {
      if (!event.cancelable) {
        return;
      }
      const area = this as unknown as DraggableAreaState;
      const touch = event.touches[0];
      if (event.touches.length > 1) {
        // a second finger: a pinch, which the image underneath handles
        area.touches = [];
        area.touchStarted = false;
        return;
      }
      if (area.movable && touch) {
        area.touches = [touch];
        area.initAnchor({ clientX: touch.clientX, clientY: touch.clientY });
        area.touchStarted = true;
        event.preventDefault();
        event.stopPropagation();
      }
    },
  },
});
</script>

<script setup lang="ts">
/**
 * The re-read dialog's selection (docs/ai/PHASE2.md §6.5): `vue-advanced-cropper`'s rectangle stencil, rebuilt from
 * the library's own parts (`BoundingBox` with its resize handlers, `DraggableArea`, `StencilPreview`) with two
 * changes: a touch drag moves the box from the first pixel, and the box takes the keyboard focus, so the dialog can
 * move and resize it with the arrow keys.
 */

interface StencilCoordinates {
  width: number;
  height: number;
  left: number;
  top: number;
}

interface Transitions {
  enabled?: boolean;
  time?: number;
  timingFunction?: string;
}

/** The props the cropper gives every stencil, plus the selection's accessible name and description */
const props = withDefaults(defineProps<{
  image?: object;
  coordinates?: object;
  stencilCoordinates: StencilCoordinates;
  transitions?: Transitions | null;
  movable?: boolean;
  resizable?: boolean;
  label?: string | null;
  describedBy?: string | null;
}>(), {
  image: undefined,
  coordinates: undefined,
  transitions: null,
  movable: true,
  resizable: true,
  label: null,
  describedBy: null,
});

const emit = defineEmits<{
  (e: "move" | "resize", event: unknown): void;
  (e: "move-end" | "resize-end"): void;
}>();

const moving = ref(false);
const resizing = ref(false);

const style = computed(() => {
  const { width, height, left, top } = props.stencilCoordinates;
  const value: Record<string, string> = {
    width: `${width}px`,
    height: `${height}px`,
    transform: `translate(${left}px, ${top}px)`,
  };
  if (props.transitions?.enabled) {
    value.transition = `${props.transitions.time}ms ${props.transitions.timingFunction}`;
  }
  return value;
});

function onMove(event: unknown) {
  emit("move", event);
  moving.value = true;
}

function onMoveEnd() {
  emit("move-end");
  moving.value = false;
}

function onResize(event: unknown) {
  emit("resize", event);
  resizing.value = true;
}

function onResizeEnd() {
  emit("resize-end");
  resizing.value = false;
}

/** The cropper asks its stencil for aspect ratio limits: the selection has none */
function aspectRatios() {
  return { minimum: undefined, maximum: undefined };
}

defineExpose({ aspectRatios });
</script>

<style scoped>
.ingest-region-stencil:focus-visible {
  outline: 3px solid rgb(var(--v-theme-primary));
  outline-offset: 2px;
}
</style>
