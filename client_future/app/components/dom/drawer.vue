<template>
    <transition>
        <dialog ref="drawerDom" @click.stop="drawerButtonHideFunc($event)">
            <div>
                <template v-for="(item, index) in 9">
                    <div class="item">
                        <span>{{item}}</span>
                    </div>
                </template>
            </div>
        </dialog>
    </transition>
    <div v-show="drawerButtonShow"
        class="drawerButtonShow"
        @click.stop="drawerButtonShowFunc($event)"
    ></div>
</template>

<script lang="ts" setup>
    const drawerDom: Ref<HTMLDialogElement | undefined> = ref();
    const drawerShow: Ref<boolean> = ref(false);
    const drawerButtonShow: Ref<boolean> = ref(true);

    function drawerButtonShowFunc(event: MouseEvent){
        if(drawerDom.value==null) return;
        drawerDom.value.showModal();
    }

    function drawerButtonHideFunc(event: Event){
        console.log(event.target);
        if(event.target===drawerDom.value) {
            drawerDom.value.close();
        };
    }
</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .drawerButtonShow{
        $size: 2.5rem;
        position: absolute;
        @include common.fixedSquare($size);
        border: 1px solid black;
        border-radius: 50%;
        transform: translate(-50%, -50%);
        top: 50%;
        left: 0rem;
        cursor: pointer;

        &::after{
            display: inline-block;
            content: "";
            border: 1px solid black;
            @include common.fixedSquare(math.div($size, 3));
            position: absolute;
            right: math.div($size, 4);
            top: math.div($size, 3);
            transform: rotate(45deg);
            border-bottom: transparent;
            border-left: transparent;
            pointer-events: none;
        }
    }

    dialog{
        position: absolute;
        top: 0%;
        left: 0%;
        @include common.fixedWidthFullHeight(50%);
        border: none !important;
        border-radius: 0 1rem 1rem 0;
        opacity: 0;
        pointer-events: none;
        overflow: hidden;

        >div{
            @include common.fullInParent();
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(10rem, 1fr));
            grid-template-rows: repeat(auto-fill, 3rem);
            grid-auto-flow: row dense;
            gap: 0.5rem;

            >.item{

            }
        }

        &[open] {
            opacity: 1;
            pointer-events: all; 
        }
    }
</style>