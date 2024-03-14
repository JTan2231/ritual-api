create database if not exists ritual;
use ritual;

CREATE TABLE if not exists user (
    user_id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    password VARCHAR(255) NOT NULL,
    UNIQUE KEY unique_username (username)
);

CREATE TABLE if not exists activity (
    activity_id INT AUTO_INCREMENT PRIMARY KEY,
    user_id int not null,
    name varchar(256) NOT NULL,
    activity_begin timestamp NOT NULL,
    activity_end timestamp not null,
    memo VARCHAR(512) NOT NULL,

    constraint fk_user foreign key (user_id) references user(user_id)
);
