export DATA_PATH=/home/jonatas/data/lavse/
export OUT_PATH=runs/temp/

python train.py \
--data_path $DATA_PATH \
--train_data f30k_precomp.en \
--val_data f30k_precomp.en \
--outpath $OUT_PATH/adaptive/f30k_precomp.en/ \
--sim adaptive \
--val_step 100 \
--workers 0 \
--image_encoder scan \
--text_encoder attngru \
--text_pooling none \
--image_pooling none \
--lr 5e-4 \
--beta 0.995 \
# --eval_before_training \
