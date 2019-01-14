#
RUNNUM=8
RUNTIME=600
ITER=100000
L1_WEIGHT=0.1
MODEL=wgan
ARCH=5 # 0: DCGAN, 1: DCGAN-BN, 2: MLP

python gan_SR.py \
       --mode=$MODEL \
       --architecture=$ARCH \
       --summary_dir=summary/$RUNNUM \
       --train_dir=train/$RUNNUM \
       --max_runtime=$RUNTIME \
       --max_iter=$ITER \
       --gen_l1_weight=$L1_WEIGHT \
       > run.log \
       2> err.log

# finish
sudo shutdown -h now
